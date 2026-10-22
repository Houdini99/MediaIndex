"""Streaming downloader for direct media links.

A link that already points at a file ('…/cat.gif') needs no resolver — it
just needs fetching. That is this module; platform pages (YouTube, TikTok,
…) go to resolver.py instead.

The fetch itself is bots_common/download.py, shared with the Discord bot:
streamed to disk, public addresses only on every redirect hop, and capped
at TELEGRAM_LINK_MAX_MB. There is no Discord-style CDN re-signing here:
nothing on this path talks to Discord's signed, expiring attachment host,
so a 403/404 is simply a failure.
"""
import aiohttp

from bots_common import sniff
from bots_common.download import fetch
# Re-exported so the bot catches `download.Blocked` / `download.TooLarge`.
from bots_common.download import TooLarge  # noqa: F401
from bots_common.sniff import Blocked  # noqa: F401


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
        serve one — one HEAD each, every hop past `require_public` first
        (see bots_common/sniff.py)."""
        return await sniff.sniff_many(self.session(), urls, allowed_ext)

    async def download(self, url, dst, label, max_bytes):
        """Stream `url` to `dst`, refusing anything over `max_bytes`.

        Raises Blocked for a URL that is not on the public internet,
        TooLarge for an oversized file, and aiohttp's own errors for
        everything else; the caller turns all three into a reply.
        """
        await fetch(self.session(), url, dst, label, max_bytes)
