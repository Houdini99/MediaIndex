"""Downloading the media a Discord message carries.

Split out of discord_bot/bot.py, which held the gateway client, this
downloader, the retry driver, the reaction vocabulary and the log-channel
formatting in one 712-line module. Nothing here
touches Discord's gateway or the bot's own state — it owns one aiohttp
session and knows how to get bytes onto disk — so it lifts out whole.

The fetch itself is bots_common/download.py, shared with the Telegram
bot: streamed to disk in 1 MiB chunks (discord.py's `att.save()` would
read a large forwarded video into memory first), and public addresses
only on every redirect hop. That last part matters for links: a message
may carry any URL, and this container sits on `proxy` and `backend`.

What this module adds on top:

  * **A cap for links.** A URL on Discord's own CDN is an attachment,
    already bounded by Discord's upload limit; anything else is capped at
    DISCORD_LINK_MAX_MB.
  * **Re-signing.** Discord serves attachments behind short-lived signed
    URLs, and forwarded or embedded media often arrives as a bare,
    already-invalid link. A 403/404 from a Discord CDN host is therefore
    not fatal: ask Discord to re-sign the URL and try once more.
"""
import logging

import aiohttp

from bots_common.download import fetch

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
        """Stream a URL to disk, capped unless it is on Discord's CDN.
        Raises sniff.Blocked, download.TooLarge (bots_common) or aiohttp's
        own errors."""
        cap = None if parsing.is_discord_cdn(url) else config.LINK_MAX_BYTES
        await fetch(self.session(), url, dst, label, cap)

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
