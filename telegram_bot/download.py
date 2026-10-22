"""Streaming downloader for direct media links.

A link that already points at a file ('…/cat.gif') needs no resolver — it
just needs fetching. That is this module; platform pages (YouTube, TikTok,
…) go to resolver.py instead.

The session and the fetch itself are bots_common/download.py, shared
with the Discord bot: streamed to disk, public addresses only on every
redirect hop, and capped at TELEGRAM_LINK_MAX_MB. There is no
Discord-style CDN re-signing here: nothing on this path talks to
Discord's signed, expiring attachment host, so a 403/404 is simply a
failure.
"""
from bots_common.download import Downloader, fetch
# Re-exported so the bot catches `download.Blocked` / `download.TooLarge`.
from bots_common.download import TooLarge  # noqa: F401
from bots_common.sniff import Blocked  # noqa: F401


class LinkDownloader(Downloader):
    """The shared Downloader with the cap the caller names. Closed by the
    application's shutdown hook."""

    async def download(self, url, dst, label, max_bytes):
        """Stream `url` to `dst`, refusing anything over `max_bytes`.

        Raises Blocked for a URL that is not on the public internet,
        TooLarge for an oversized file, and aiohttp's own errors for
        everything else; the caller turns all three into a reply.
        """
        await fetch(self.session(), url, dst, label, max_bytes)
