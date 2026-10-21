"""Async client for the index's upload endpoint.

Multipart POST to ``/api/upload`` with HTTP Basic auth — the index
account's *app password*, generated on the index's /queue page — and a
JSON reply naming the item a duplicate collided with, so the bot can
report *what* it duplicated.

The app password is a bearer credential sent on every request, so there
is no login step and no session to keep alive. What remains of the
IP-ban guard (10 failed logins in 15 minutes bans the container for 24
hours) is the cooldown: after a rejected credential, callers are refused
locally instead of being retried against the server.

`lookup_discord_user` asks which index account linked a Discord user id,
and `verify_discord_link` redeems the link code a user DMs the bot — the
bot holds no database credentials of its own. `site_login` fetches the
stored login yt-dlp downloads a link with, and `store_site_login` saves
what the site rotated it to (see bots_common/site_logins.py).
"""
import logging
import time

import aiohttp

from bots_common.site_logins import StoredLogin


class AuthError(Exception):
    """Login failed or the session cannot be established — the caller
    must back off before retrying (failed logins feed the IP ban)."""


class IndexUnavailable(Exception):
    """The identity lookup got no answer: the index was unreachable, or
    answered with an error. Distinct from "not linked" on purpose — telling
    a linked user to go and link their account because the web app was
    restarting would send them to fix something that is not broken."""


class UploadResult:
    """Outcome of one upload.

    `status` is 'ok' | 'duplicate' | 'error'. `detail` carries the index's
    own reason on failure, and `retryable` says whether that failure was
    the index having a bad moment (5xx, unreadable reply) rather than a
    verdict on the file — the bot's retry queue reads it.

    `duplicate` describes the already-indexed item a rejected duplicate
    collided with
    ({'id', 'is_private', …}), or None when the index could not name one.
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
    def __init__(self, base_url, username, app_password, auth_cooldown=900):
        self._base = base_url.rstrip('/')
        self._username = username
        self._app_password = app_password
        self._cooldown = auth_cooldown
        self._session = None
        self._blocked_until = 0.0

    def _http(self):
        # No cookie-jar configuration: the app password is sent per request
        # and nothing here holds a session. The old jar had to be told to
        # treat the plain-http internal origin as secure, because the Flask
        # session cookie is Secure-flagged and would otherwise have been
        # dropped on every hop over http://web:5001.
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600),
            )
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── Credential cooldown ────────────────────────────────────────────
    def _block(self):
        """Stop trying this credential for a while. Called on every
        rejection so a burst of ingests can't spend the ban budget."""
        self._blocked_until = time.monotonic() + self._cooldown

    def _raise_if_blocked(self):
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise AuthError(
                f"the index rejected these credentials; not retrying for "
                f"another {remaining:.0f}s (repeated failures would IP-ban "
                f"this container)")

    async def _upload_api(self, path, filename, tags, uploaded_for, private):
        self._raise_if_blocked()
        form = aiohttp.FormData()
        form.add_field('tags', tags)
        if uploaded_for:
            form.add_field('uploaded_for', uploaded_for)
        if private:
            form.add_field('private', '1')
        auth = aiohttp.BasicAuth(self._username, self._app_password)
        with open(path, 'rb') as fh:
            form.add_field('file', fh, filename=filename,
                           content_type='application/octet-stream')
            async with self._http().post(f'{self._base}/api/upload',
                                         data=form, auth=auth) as resp:
                if resp.status in (401, 403):
                    self._block()
                    raise AuthError(
                        f"the index rejected the app password for "
                        f"{self._username!r} (HTTP {resp.status}) — check "
                        f"INDEX_BOT_APP_PASSWORD, and regenerate it on the "
                        f"index's /queue page if needed")
                if resp.status == 404:
                    self._block()
                    raise AuthError(
                        "this index has no /api/upload endpoint — upgrade "
                        "the index")
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

    async def lookup_discord_user(self, discord_id):
        """Index username that linked this Discord user id, or None.

        Only a 404 is "not linked". Anything else that leaves us without
        an answer raises IndexUnavailable, and a rejected app password
        raises AuthError and starts the cooldown, exactly as an upload does.
        """
        return await self._identity_call(
            'GET', '/api/discord/user', not_found=(404,),
            params={'discord_id': str(discord_id)})

    async def verify_discord_link(self, discord_id, code):
        """Redeem a link code the user DMed us, on behalf of `discord_id`
        (the DM's sender as Discord reported it). Returns the index username
        it linked, or None for a wrong, used or expired code — the index
        answers 400 for a code that is not even well-formed. Raises like
        lookup_discord_user otherwise."""
        return await self._identity_call(
            'POST', '/api/discord/verify', not_found=(400, 404),
            data={'discord_id': str(discord_id), 'code': code})

    async def site_login(self, url):
        """The stored login (a StoredLogin) to download `url` with — its
        site's own, or the index's "other sites" fallback — or None when
        neither is stored. An index from before site logins has no such
        endpoint and answers 404, which means the same thing. Raises like
        lookup_discord_user otherwise."""
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
        revision, or None when the index kept its own copy — an admin
        replaced the login meanwhile (409), the new file would have logged
        the site out (422), or it held nothing for the site (400)."""
        data = await self._json_call(
            'PUT', f'/api/site-logins/{login.platform}/cookies',
            not_found=(400, 404, 409, 422),
            json={'revision': login.revision, 'cookies': text})
        return data.get('revision') if data else None

    async def site_logins(self):
        """[{'platform', 'name', 'logged_in', 'readable'}, …] for every
        stored login — names only, for the startup line."""
        data = await self._json_call('GET', '/api/site-logins',
                                     not_found=(404,))
        return list((data or {}).get('logins') or [])

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

    async def _json_call(self, method, path, not_found, **kw):
        """One authenticated request; its JSON object, or None for a status
        in `not_found`. A rejected app password raises AuthError and starts
        the cooldown; no answer at all raises IndexUnavailable."""
        self._raise_if_blocked()
        auth = aiohttp.BasicAuth(self._username, self._app_password)
        send = getattr(self._http(), method.lower())
        try:
            async with send(f'{self._base}{path}', auth=auth, **kw) as resp:
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

    async def upload(self, path, filename, tags, uploaded_for=None,
                     private=False):
        """Upload one file and return an UploadResult.

        `uploaded_for` names the index user who should own the upload —
        the app honors it only for accounts in its DELEGATED_UPLOADERS.
        `private` stores it encrypted under that owner's key, visible to
        nobody else.

        One file per request keeps the result unambiguous. Raises
        AuthError when the app password is rejected.
        """
        return await self._upload_api(path, filename, tags, uploaded_for,
                                      private)
