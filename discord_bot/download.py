"""Downloading media off the Discord CDN.

Split out of discord_bot/bot.py, which held the gateway client, this
downloader, the retry driver, the reaction vocabulary and the log-channel
formatting in one 712-line module. Nothing here
touches Discord's gateway or the bot's own state — it owns one aiohttp
session and knows how to get bytes onto disk — so it lifts out whole.

Two non-obvious behaviours it exists to preserve:

  * **Streaming, not buffering.** discord.py's `att.save()` reads the whole
    file into memory first, which turns one large forwarded video into an
    OOM risk for the container. Everything here goes to disk in 1 MiB
    chunks.
  * **Re-signing.** Discord serves attachments behind short-lived signed
    URLs, and forwarded or embedded media often arrives as a bare,
    already-invalid link. A 403/404 from a Discord CDN host is therefore
    not fatal: ask Discord to re-sign the URL and try once more.
"""
import logging
import time

import aiohttp

from bots_common import resolver

from . import config, parsing

REFRESH_URL_ENDPOINT = 'https://discord.com/api/v10/attachments/refresh-urls'

#: Statuses that mean "this signed URL expired", as opposed to a real error.
_EXPIRED_STATUSES = (403, 404)


class CdnDownloader:
    """Owns the download session for the lifetime of the bot.

    The session is created lazily on first use and must be closed by the
    caller (`IndexBot.close`), which is why it is an object rather than a
    handful of module-level functions.
    """

    def __init__(self):
        self._session = None

    def session(self):
        """Lazy aiohttp session for attachment downloads. No total timeout —
        a 400 MB video takes as long as it takes — but a stalled read dies."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=30,
                                              sock_read=120))
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def stream(self, url, dst, label):
        """Stream a URL to disk in 1 MiB chunks (reopens dst each call, so a
        retry cleanly overwrites a partial first attempt). Progress lands in
        the log every few seconds so a big download never looks like a hang;
        quick ones stay silent."""
        async with self.session().get(url) as resp:
            resp.raise_for_status()
            total = resp.content_length
            done = 0
            started = time.monotonic()
            next_log = started + resolver.PROGRESS_INTERVAL
            with open(dst, 'wb') as fh:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
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

    async def download(self, src, dst):
        """Download one source to disk, re-signing an expired Discord CDN
        URL once before giving up."""
        try:
            await self.stream(src.url, dst, src.filename)
            return
        except aiohttp.ClientResponseError as e:
            if e.status not in _EXPIRED_STATUSES or not parsing.is_discord_cdn(src.url):
                raise
            first_error = e
            logging.info("download: %s HTTP %s — re-signing Discord CDN URL",
                         src.filename, e.status)
        fresh = await self.refresh_cdn_url(src.url)
        if not fresh:
            raise first_error
        await self.stream(fresh, dst, src.filename)

    async def refresh_cdn_url(self, url):
        """Re-sign a Discord CDN attachment URL via POST
        /attachments/refresh-urls — works even for a bare/expired link when
        we don't hold the Attachment object (forwarded or embedded media)."""
        headers = {'Authorization': f'Bot {config.BOT_TOKEN}'}
        try:
            async with self.session().post(
                    REFRESH_URL_ENDPOINT,
                    json={'attachment_urls': [url]}, headers=headers) as resp:
                if resp.status != 200:
                    logging.warning("refresh-urls: HTTP %s for %s",
                                    resp.status, url)
                    return None
                data = await resp.json()
        except Exception:
            logging.warning("refresh-urls: request failed", exc_info=True)
            return None
        for item in data.get('refreshed_urls') or []:
            if item.get('refreshed'):
                return item['refreshed']
        return None
