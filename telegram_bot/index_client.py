"""Async client for the index's JSON API.

A sibling of discord_bot/index_client.py rather than a shared module — the
two have diverged — but faithful to it where the behaviour is
load-bearing:

* HTTP Basic with the index account's *app password*, generated on the
  index's /queue page. A bearer credential sent per request: no login
  step, no session to keep alive.
* A rejected credential blocks this client locally for a cooldown instead
  of being retried. Ten failed logins in fifteen minutes get the
  container's IP banned for twenty-four hours, and a burst of ingests
  must not be able to spend that budget in one second.

What is new here is `lookup_telegram_user`, the ingest gate: the bot has
no database of its own (Postgres is confined to the `backend` network), so
"is this sender allowed to upload?" is a question for the web app. The
same goes for the site logins yt-dlp downloads with: `site_login` fetches
the one a link needs, `store_site_login` saves what the site rotated it to
(see bots_common/site_logins.py).
"""
import logging
import time

import aiohttp

from bots_common.site_logins import StoredLogin


class AuthError(Exception):
    """The app password was rejected — the caller must back off before
    retrying, because failed attempts feed the IP ban."""


class IndexUnavailable(Exception):
    """The index could not be reached, or answered with a 5xx.

    Distinct from "not linked" on purpose: both leave the caller without
    an owner, but only one of them is the *user's* situation. Telling a
    linked user they are not linked because the web app was restarting
    would send them to the settings page to fix something that is not
    broken.
    """


class UploadResult:
    """Outcome of one upload.

    `status` is 'ok' | 'duplicate' | 'error'. `retryable` says whether a
    failure was the index having a bad moment (5xx, unreadable reply)
    rather than a verdict on the file. `duplicate` describes the
    already-indexed item a rejected duplicate collided with, or None —
    the index withholds everything but the id for private items, on
    purpose.
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
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600),
            )
        return self._session

    def _auth(self):
        return aiohttp.BasicAuth(self._username, self._app_password)

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── Credential cooldown ────────────────────────────────────────────
    def _block(self):
        self._blocked_until = time.monotonic() + self._cooldown

    def _raise_if_blocked(self):
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise AuthError(
                f"the index rejected these credentials; not retrying for "
                f"another {remaining:.0f}s (repeated failures would IP-ban "
                f"this container)")

    # ── Identity lookup (the ingest gate) ──────────────────────────────
    async def lookup_telegram_user(self, telegram_id, username=None):
        """Index username that claimed this Telegram account, or None.

        None means "not linked" and the caller must drop the message
        without downloading anything.

        Only a 404 is "not linked". Anything else that leaves us without
        an answer raises IndexUnavailable, so the gate still fails closed —
        nothing is downloaded — without the caller mistaking an outage for
        a verdict on the sender.
        """
        self._raise_if_blocked()
        params = {'telegram_id': str(telegram_id)}
        if username:
            params['username'] = username
        try:
            async with self._http().get(f'{self._base}/api/telegram/user',
                                        params=params,
                                        auth=self._auth()) as resp:
                if resp.status in (401, 403):
                    self._block()
                    raise AuthError(
                        f"the index rejected the app password for "
                        f"{self._username!r} (HTTP {resp.status}) — check "
                        f"INDEX_BOT_APP_PASSWORD, and make sure that account "
                        f"is listed in the index's DELEGATED_UPLOADERS")
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    logging.error("index: telegram lookup failed with HTTP %s",
                                  resp.status)
                    raise IndexUnavailable(
                        f"the index returned HTTP {resp.status}")
                data = await resp.json(content_type=None)
                username = data.get('username')
                if not username:
                    raise IndexUnavailable("the index sent an unreadable reply")
                return username
        except (aiohttp.ClientError, TimeoutError) as e:
            logging.warning("index: telegram lookup unreachable: %s", e)
            raise IndexUnavailable("the index is unreachable") from e

    # ── Site logins (the link resolver) ────────────────────────────────
    async def site_login(self, url):
        """The stored login (a StoredLogin) to download `url` with, or None
        when the index holds none for its site — or has no such endpoint
        yet (404 either way). Raises AuthError / IndexUnavailable like the
        identity lookup, so an outage is never mistaken for "no login"."""
        data = await self._json_call('GET', '/api/site-logins/cookies',
                                     not_found=(404,), params={'url': url})
        if data is None:
            return None
        login = StoredLogin.from_json(data)
        if login is None:
            raise IndexUnavailable("the index sent an unreadable reply")
        return login

    async def store_site_login(self, login, text):
        """Save the cookies a download rotated `login` to. The new revision,
        or None when the index kept its own copy (changed meanwhile, would
        have logged the site out, or held nothing for the site)."""
        data = await self._json_call(
            'PUT', f'/api/site-logins/{login.platform}/cookies',
            not_found=(400, 404, 409, 422),
            json={'revision': login.revision, 'cookies': text})
        return data.get('revision') if data else None

    async def _json_call(self, method, path, not_found, **kw):
        """One authenticated request; its JSON object, or None for a status
        in `not_found`."""
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
        app honors it only for accounts in its DELEGATED_UPLOADERS. One
        file per request keeps the result unambiguous.
        """
        self._raise_if_blocked()
        form = aiohttp.FormData()
        form.add_field('tags', tags)
        if private:
            form.add_field('private', '1')
        if uploaded_for:
            form.add_field('uploaded_for', uploaded_for)
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
