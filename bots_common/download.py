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
    library. Redirects are followed by hand so every hop is checked.
  * **A cap that is enforced twice.** `Content-Length` is checked before a
    byte is written, and the running total is checked again during the
    transfer: a chunked response carries no length to trust, and the
    sender chooses the URL.

— while each bot keeps its own session and what it does around the fetch
(the Discord bot re-signs an expired CDN link and tries once more).
"""
import asyncio
import logging
import time
import urllib.parse

from . import parsing
from .resolver import PROGRESS_INTERVAL
from .sniff import MAX_REDIRECTS, REDIRECTS, Blocked, require_public


class TooLarge(Exception):
    """The response is over the configured cap. Carries the size when the
    server declared one, so the reply can name it."""

    def __init__(self, size=None):
        super().__init__("the file is over the size limit")
        self.size = size


async def fetch(session, url, dst, label, max_bytes=None):
    """Stream `url` to `dst`, refusing anything over `max_bytes` (None
    means no cap). `dst` is reopened each call, so a retry cleanly
    overwrites a partial first attempt.

    Raises Blocked for a URL that is not on the public internet, TooLarge
    for an oversized file, and aiohttp's own errors (ClientResponseError
    for an error status) for everything else.
    """
    for _ in range(MAX_REDIRECTS + 1):
        # getaddrinfo blocks; a slow resolver must not stall the bot.
        await asyncio.to_thread(require_public, url)
        async with session.get(url, allow_redirects=False) as resp:
            location = resp.headers.get('Location')
            if resp.status in REDIRECTS and location:
                url = urllib.parse.urljoin(url, location)
                continue
            await _stream(resp, dst, label, max_bytes)
            return
    raise Blocked(f"that link redirected more than {MAX_REDIRECTS} times")


async def _stream(resp, dst, label, max_bytes):
    """Write one response to disk. Progress lands in the log every few
    seconds so a big download never looks like a hang; quick ones stay
    silent."""
    resp.raise_for_status()
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
