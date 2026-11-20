"""Fetching a URL a bot was handed, onto disk.

Both bots download files they were sent a link to: the Discord bot for
attachments, embeds and links in a message, the Telegram bot for links.
They share how that fetch is made —

  * **Streaming, not buffering.** Bytes go to disk in 1 MiB chunks. The
    obvious `await resp.read()` would put a whole video in the container's
    memory first, and the bot containers are deliberately small.
  * **Public addresses only.** The bots sit on `proxy` and `backend`, so
    without `require_public` (bots_common/sniff.py), a link ending in
    `.jpg` would be a way to make one fetch `http://web:5001/…`, or any of
    the stack's internal services, and put the answer in the sender's
    library. Redirects are followed by hand (sniff.follow_public) so
    every hop is checked.
  * **A cap that is enforced twice.** `Content-Length` is checked before a
    byte is written, and the running total is checked again during the
    transfer: a chunked response carries no length to trust, and the
    sender chooses the URL.
  * **A file, not a page.** A link can end in `.mp4` and still answer
    with a web page (a file host's viewer, a login wall). An HTML answer
    is refused rather than saved under the video's name.
  * **Hosts that sign their files.** A Bunkr link is a page whose file
    needs a fresh token; fetch() asks for one right before the transfer
    (bots_common/bunkr.py). A Bunkr album is read into its files' links
    first (Downloader.read_albums).

— and the Downloader that owns the session both use. Each bot subclasses
it with what it does around the fetch (the Discord bot re-signs an
expired CDN link and tries once more).
"""
import asyncio
import logging
import time

import aiohttp

from . import bunkr, parsing, sniff
from .resolver import PROGRESS_INTERVAL

#: Content types that mean "you got the web page, not the file".
_PAGE_TYPES = frozenset({'text/html', 'application/xhtml+xml'})


class TooLarge(Exception):
    """The response is over the configured cap. Carries the size when the
    server declared one, so the reply can name it."""

    def __init__(self, size=None):
        super().__init__("the file is over the size limit")
        self.size = size


class NotAFile(Exception):
    """The link answered with a web page instead of the file it seemed to
    name, or with a page the bot could not find the file on. The message
    is shown to the sender."""


class Downloader:
    """Owns a bot's download session for the bot's lifetime.

    The session is created lazily on first use and closed by the bot's
    shutdown, which is why this is an object rather than a handful of
    free functions.
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
        """{url: (fetch_url, filename)} for the first sniff.MAX_PER_MESSAGE
        distinct `urls` that name no file but serve one, asked in
        parallel — one HEAD each, every hop past `require_public` first
        (see bots_common/sniff.py).

        A Bunkr link is asked through Bunkr's own API instead, since a HEAD
        only ever finds its page, and keeps the link as the URL to fetch:
        its file needs a token minted at download time (see fetch)."""
        urls = list(dict.fromkeys(urls))[:sniff.MAX_PER_MESSAGE]
        session = self.session()
        found = await asyncio.gather(*(
            (bunkr.sniff_link if bunkr.is_file_link(url) else sniff.sniff)(
                session, url, allowed_ext)
            for url in urls))
        return {url: hit for url, hit in zip(urls, found) if hit}

    async def read_albums(self, urls, allowed_ext, limit):
        """{url: bunkr.Album} for the Bunkr album links among `urls` — the
        first sniff.MAX_PER_MESSAGE distinct ones, read in parallel, one
        request each. An album's files are ordinary Bunkr file links, so
        they download through fetch like any other link; `limit` caps how
        many are taken from each album."""
        albums = [url for url in dict.fromkeys(urls)
                  if bunkr.is_album_link(url)][:sniff.MAX_PER_MESSAGE]
        if not albums:
            return {}
        session = self.session()
        found = await asyncio.gather(*(
            bunkr.read_album(session, url, allowed_ext, limit)
            for url in albums))
        return dict(zip(albums, found))


async def fetch(session, url, dst, label, max_bytes=None):
    """Stream `url` to `dst`, refusing anything over `max_bytes` (None
    means no cap). `dst` is reopened each call, so a retry cleanly
    overwrites a partial first attempt.

    A Bunkr link is swapped for a freshly signed link to its file first.

    Raises Blocked for a URL that is not on the public internet, TooLarge
    for an oversized file, NotAFile for a web page where the file should
    be, and aiohttp's own errors (ClientResponseError for an error status)
    for everything else.
    """
    if bunkr.is_file_link(url):
        try:
            url = await bunkr.file_url(session, url)
        except bunkr.Unreadable as e:
            raise NotAFile(str(e)) from e
    async with sniff.follow_public(session, 'GET', url) as (_, resp):
        await _stream(resp, dst, label, max_bytes)


async def _stream(resp, dst, label, max_bytes):
    """Write one response to disk. Progress lands in the log every few
    seconds so a big download never looks like a hang; quick ones stay
    silent."""
    resp.raise_for_status()
    ctype = (resp.headers.get('Content-Type') or '').split(';')[0]
    if ctype.strip().lower() in _PAGE_TYPES:
        logging.info("download: %s — the link answered with a web page (%s)",
                     label, ctype)
        raise NotAFile("that link opens a web page, not the file itself")
    total = resp.content_length
    if max_bytes and total and total > max_bytes:
        # Declared too big: give up before writing anything.
        raise TooLarge(total)
    done = 0
    started = time.monotonic()
    next_log = started + PROGRESS_INTERVAL
    with open(dst, 'wb') as fh:
        async for chunk in resp.content.iter_chunked(1 << 20):
            done += len(chunk)
            if max_bytes and done > max_bytes:
                # No Content-Length, or a lying one. Stop here rather than
                # filling the disk; the caller deletes the partial file.
                raise TooLarge()
            fh.write(chunk)
            now = time.monotonic()
            if now >= next_log:
                next_log = now + PROGRESS_INTERVAL
                logging.info(
                    "download: %s — %s", label,
                    parsing.format_progress(done, total,
                                            done / (now - started)))
    if time.monotonic() - started >= PROGRESS_INTERVAL:
        logging.info("download: %s — complete (%.1f MB)", label, done / 1e6)
