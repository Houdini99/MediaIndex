"""Streaming downloader for direct media links.

A link that already points at a file ('…/cat.gif') needs no resolver — it
just needs fetching. That is this module; platform pages (YouTube, TikTok,
…) go to resolver.py instead.

A sibling of discord_bot/download.py, minus its Discord CDN re-signing:
nothing here is talking to Discord's signed, expiring attachment host, so
a 403/404 is simply a failure. What is kept is the part that matters —

  * **Streaming, not buffering.** Bytes go to disk in 1 MiB chunks. The
    obvious `await resp.read()` would put a whole video in the container's
    memory first, and this container is deliberately small.
  * **A cap that is enforced twice.** `Content-Length` is checked before a
    byte is written, and the running total is checked again during the
    transfer — a chunked response carries no length to trust, and the
    sender chooses the URL.

— and one thing the Discord one does not do. There is no extractor gate on
this path (that is resolver.py's control): whatever URL arrives is
requested as-is. This container sits on `proxy` and `backend`, so without
`_require_public` (from bots_common/sniff.py), a link ending in `.jpg`
would be a way to make it fetch `http://web:5001/…` — or any of the
stack's internal services — and put the answer in the sender's library.
"""
import asyncio
import logging
import time
import urllib.parse

import aiohttp

from bots_common import resolver, sniff
# The public-address bar is shared with the Discord bot's link sniffing.
from bots_common.sniff import MAX_REDIRECTS, Blocked
from bots_common.sniff import REDIRECTS as _REDIRECTS
from bots_common.sniff import require_public as _require_public

from . import parsing


class TooLarge(Exception):
    """The response is over the configured cap. Carries the size when the
    server declared one, so the reply can name it."""

    def __init__(self, size=None):
        super().__init__("the file is over the size limit")
        self.size = size


class LinkDownloader:
    """Owns the download session for the lifetime of the bot.

    Created lazily on first use and closed by the application's shutdown
    hook, which is why this is an object rather than a free function.
    """

    def __init__(self):
        self._session = None

    def session(self):
        """Lazy aiohttp session. No total timeout — a 400 MB video takes as
        long as it takes — but a stalled read dies."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=30,
                                              sock_read=120))
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def sniff_links(self, urls, allowed_ext):
        """{url: (fetch_url, filename)} for links that name no file but
        serve one — one HEAD each, every hop past `_require_public` first
        (see bots_common/sniff.py)."""
        return await sniff.sniff_many(self.session(), urls, allowed_ext)

    async def download(self, url, dst, label, max_bytes):
        """Stream `url` to `dst`, refusing anything over `max_bytes`.

        Raises Blocked for a URL that is not on the public internet,
        TooLarge for an oversized file, and aiohttp's own errors for
        everything else; the caller turns all three into a reply.

        Redirects are followed by hand rather than by aiohttp, because
        each hop has to go back through `_require_public` — the whole
        check is worth nothing if a public URL may 302 to an internal one.
        """
        for _ in range(MAX_REDIRECTS + 1):
            # getaddrinfo blocks; a slow resolver must not stall the bot.
            await asyncio.to_thread(_require_public, url)
            async with self.session().get(url, allow_redirects=False) as resp:
                location = resp.headers.get('Location')
                if resp.status in _REDIRECTS and location:
                    url = urllib.parse.urljoin(url, location)
                    continue
                await self._stream(resp, dst, label, max_bytes)
                return
        raise Blocked(f"that link redirected more than {MAX_REDIRECTS} times")

    @staticmethod
    async def _stream(resp, dst, label, max_bytes):
        """Write one response to disk. Progress lands in the log every few
        seconds so a big download never looks like a hang; quick ones stay
        silent."""
        resp.raise_for_status()
        total = resp.content_length
        if total and total > max_bytes:
            # Declared too big: give up before writing anything.
            raise TooLarge(total)
        done = 0
        started = time.monotonic()
        next_log = started + resolver.PROGRESS_INTERVAL
        with open(dst, 'wb') as fh:
            async for chunk in resp.content.iter_chunked(1 << 20):
                done += len(chunk)
                if done > max_bytes:
                    # No Content-Length, or a lying one. Stop here rather
                    # than filling the disk; the caller deletes the
                    # partial file.
                    raise TooLarge()
                fh.write(chunk)
                now = time.monotonic()
                if now >= next_log:
                    next_log = now + resolver.PROGRESS_INTERVAL
                    logging.info(
                        "download: %s — %s", label,
                        parsing.format_progress(done, total,
                                                done / (now - started)))
        if time.monotonic() - started >= resolver.PROGRESS_INTERVAL:
            logging.info("download: %s — complete (%.1f MB)",
                         label, done / 1e6)
