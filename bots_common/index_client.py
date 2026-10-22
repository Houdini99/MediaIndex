"""Async client for the index's JSON API, shared by both bots.

HTTP Basic with the index account's *app password*, generated on the
index's /queue page: a bearer credential sent on every request, so there
is no login step and no session to keep alive. What guards the IP ban (10
failed logins in 15 minutes ban the container for 24 hours) is the
cooldown: after a rejected credential, callers are refused locally
instead of being retried against the server, so a burst of ingests can't
spend that budget in one second. It is the bots' only backoff clock:
they ask `blocked()` before starting work.

The bots hold no database credentials (Postgres is confined to the
`backend` network), so "which index account linked this chat account?"
is a question for the web app. Each bot subclasses IndexClient with its
own lookup (discord_bot/index_client.py, telegram_bot/index_client.py);
the answers are cached here. `upload` sends one file, `site_login`
fetches the stored login yt-dlp downloads a link with, and
`store_site_login` saves what the site rotated it to (see
bots_common/site_logins.py).
"""
import logging
import time

import aiohttp

from .site_logins import StoredLogin


class AuthError(Exception):
    """The app password was rejected. The caller must back off before
    retrying, because failed attempts feed the IP ban."""


class IndexUnavailable(Exception):
    """The index gave no usable answer: it was unreachable, answered with
    an error, or sent an unreadable reply.

    Distinct from "not linked" on purpose: both leave the caller without
    an owner, but only one of them is the *user's* situation. Telling a
    linked user to go and link their account because the web app was
    restarting would send them to fix something that is not broken.
    """


class UploadResult:
    """Outcome of one upload.

    `status` is 'ok' | 'duplicate' | 'error'. `detail` carries the index's
    own reason on failure, and `retryable` says whether that failure was
    the index having a bad moment (5xx, unreadable reply) rather than a
    verdict on the file; the bots' retry paths read it.

    `duplicate` describes the already-indexed item a rejected duplicate
    collided with ({'id', 'is_private', …}), or None when the index could
    not name one. It withholds everything but the id for private items,
    on purpose.
    """
    __slots__ = ('status', 'detail', 'filehash', 'duplicate', 'retryable')

    def __init__(self, status, detail=None, filehash=None, duplicate=None,
                 retryable=False):
        self.status = status
        self.detail = detail
        self.filehash = filehash
        self.duplicate = duplicate
        self.retryable = retryable


class IndexClient:
    def __init__(self, base_url, username, app_password, auth_cooldown=900,
                 lookup_ttl=60):
        self._base = base_url.rstrip('/')
        self._username = username
        self._app_password = app_password
        self._cooldown = auth_cooldown
        self._lookup_ttl = lookup_ttl
        self._session = None
        self._blocked_until = 0.0
        self._owners = {}       # chat user id -> (index username|None, expires)

    def _http(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600),
            )
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _auth(self):
        return aiohttp.BasicAuth(self._username, self._app_password)

    # ── Credential cooldown ────────────────────────────────────────────
    def _block(self):
        """Stop trying this credential for a while. Called on every
        rejection so a burst of ingests can't spend the ban budget."""
        self._blocked_until = time.monotonic() + self._cooldown

    def blocked(self):
        """Seconds left on the cooldown, or 0. The bots ask this before
        starting work that would only end in the index refusing it."""
        return max(0.0, self._blocked_until - time.monotonic())

    def _raise_if_blocked(self):
        remaining = self.blocked()
        if remaining:
            raise AuthError(
                f"the index rejected these credentials; not retrying for "
                f"another {remaining:.0f}s (repeated failures would IP-ban "
                f"this container)")

    # ── Identity lookups ───────────────────────────────────────────────
    async def _linked_owner(self, user_id, path, params):
        """Index username that linked chat account `user_id`, or None.

        Only a 404 is "not linked". Cached for `lookup_ttl` seconds, "not
        linked" included, so a message with ten attachments, or a flood
        from one unlinked account, costs one request. Anything that leaves
        us without an answer raises (AuthError, IndexUnavailable) and is
        never cached: that would keep a linked user unlinked after the
        index came back.
        """
        cached = self._owners.get(user_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        owner = await self._identity_call('GET', path, not_found=(404,),
                                          params=params)
        self._owners[user_id] = (owner, time.monotonic() + self._lookup_ttl)
        return owner

    def _forget_owner(self, user_id):
        """Drop a cached answer that a new link made stale."""
        self._owners.pop(user_id, None)

    async def _identity_call(self, method, path, not_found, **kw):
        """One request to an identity endpoint; its `username`, or None for
        a status in `not_found`."""
        data = await self._json_call(method, path, not_found, **kw)
        if data is None:
            return None
        username = data.get('username')
        if not username:
            raise IndexUnavailable("the index sent an unreadable reply")
        return username

    # ── Site logins (the link resolver) ────────────────────────────────
    async def site_login(self, url):
        """The stored login (a StoredLogin) to download `url` with (its
        site's own, or the index's "other sites" fallback), or None when
        neither is stored. Raises AuthError / IndexUnavailable like the
        identity lookups, so an outage is never mistaken for "no login"."""
        data = await self._json_call('GET', '/api/site-logins/cookies',
                                     not_found=(404,), params={'url': url})
        if data is None:
            return None
        login = StoredLogin.from_json(data)
        if login is None:
            raise IndexUnavailable("the index sent an unreadable reply")
        return login

    async def store_site_login(self, login, text):
        """Save the cookies a download rotated `login` to. Returns the new
        revision, or None when the index kept its own copy: an admin
        replaced the login meanwhile (409), the new file would have logged
        the site out (422), or it held nothing for the site (400)."""
        data = await self._json_call(
            'PUT', f'/api/site-logins/{login.platform}/cookies',
            not_found=(400, 404, 409, 422),
            json={'revision': login.revision, 'cookies': text})
        return data.get('revision') if data else None

    async def site_logins(self):
        """[{'platform', 'name', 'logged_in', 'readable'}, …] for every
        stored login, names only, for the startup line."""
        data = await self._json_call('GET', '/api/site-logins',
                                     not_found=(404,))
        return list((data or {}).get('logins') or [])

    async def _json_call(self, method, path, not_found, **kw):
        """One authenticated request; its JSON object, or None for a status
        in `not_found`. A rejected app password raises AuthError and starts
        the cooldown; no usable answer raises IndexUnavailable."""
        self._raise_if_blocked()
        send = getattr(self._http(), method.lower())
        try:
            async with send(f'{self._base}{path}', auth=self._auth(),
                            **kw) as resp:
                if resp.status in (401, 403):
                    self._block()
                    raise AuthError(
                        f"the index rejected the app password for "
                        f"{self._username!r} (HTTP {resp.status}) — check "
                        f"INDEX_BOT_APP_PASSWORD, and make sure that account "
                        f"is listed in the index's DELEGATED_UPLOADERS")
                if resp.status in not_found:
                    return None
                if resp.status >= 400:
                    logging.error("index: %s %s failed with HTTP %s",
                                  method, path, resp.status)
                    raise IndexUnavailable(
                        f"the index returned HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            logging.warning("index: %s %s unreachable: %s", method, path, e)
            raise IndexUnavailable("the index is unreachable") from e
        except ValueError as e:
            raise IndexUnavailable("the index sent an unreadable reply") from e
        if not isinstance(data, dict):
            raise IndexUnavailable("the index sent an unreadable reply")
        return data

    # ── Upload ─────────────────────────────────────────────────────────
    async def upload(self, path, filename, tags, uploaded_for=None,
                     private=False):
        """Upload one file and return an UploadResult.

        `uploaded_for` names the index user who should own the upload; the
        app honors it only for accounts in its DELEGATED_UPLOADERS.
        `private` stores it encrypted under that owner's key, visible to
        nobody else.

        One file per request keeps the result unambiguous. Raises
        AuthError when the app password is rejected; any other 4xx is an
        error for this file, a 5xx one worth retrying.
        """
        self._raise_if_blocked()
        form = aiohttp.FormData()
        form.add_field('tags', tags)
        if uploaded_for:
            form.add_field('uploaded_for', uploaded_for)
        if private:
            form.add_field('private', '1')
        with open(path, 'rb') as fh:
            form.add_field('file', fh, filename=filename,
                           content_type='application/octet-stream')
            async with self._http().post(f'{self._base}/api/upload',
                                         data=form, auth=self._auth()) as resp:
                if resp.status in (401, 403):
                    self._block()
                    raise AuthError(
                        f"the index rejected the app password for "
                        f"{self._username!r} (HTTP {resp.status}) — check "
                        f"INDEX_BOT_APP_PASSWORD, and regenerate it on the "
                        f"index's /queue page if needed")
                if resp.status >= 400:
                    logging.error("index: upload of %s failed with HTTP %s",
                                  filename, resp.status)
                    # A 5xx is the index having a bad moment, not a verdict
                    # on the file — worth another attempt later.
                    return UploadResult(
                        'error', f"the index returned HTTP {resp.status}",
                        retryable=resp.status >= 500)
                try:
                    data = await resp.json(content_type=None)
                    result = (data.get('results') or [])[0]
                except Exception:
                    logging.exception("index: unreadable /api/upload reply "
                                      "for %s", filename)
                    return UploadResult(
                        'error', "the index sent an unreadable reply",
                        retryable=True)
        return UploadResult(result.get('status', 'error'),
                            detail=result.get('detail'),
                            filehash=result.get('filehash'),
                            duplicate=result.get('duplicate_of'))
