"""Channel A: drain the Redis event queue into Discord announcements.

The web app LPUSHes events (app/notifications.py); BRPOP here pops the
oldest first, so the feed stays chronological and survives bot downtime.
"""
import asyncio
import contextlib
import json
import logging
import os
import urllib.parse

import discord
import redis.asyncio as aioredis

from . import config, parsing

# Discord's brand palette, keyed by media kind — announcements get a
# colored accent bar and a matching title without looking off-theme.
_TYPE_STYLE = {
    'image': ('🖼️', 'image', 0x5865F2),
    'gif':   ('🎞️', 'GIF',   0xEB459E),
    'video': ('🎬', 'video', 0xED4245),
}


def _kind(ext):
    if ext == 'gif':
        return 'gif'
    return 'image' if ext in config.IMAGE_EXT else 'video'


def _fmt_size(nbytes):
    if nbytes >= 1e9:
        return f"{nbytes / 1e9:.2f} GB"
    if nbytes >= 1e6:
        return f"{nbytes / 1e6:.1f} MB"
    return f"{nbytes / 1e3:.0f} kB"


async def notify_loop(client):
    """Drain the event queue forever.

    Channel resolution lives *inside* the retry loop on purpose: it used to
    run once before it, so a single transient HTTPException at startup
    killed the loop and left the process running with the announcement feed
    silently dead until someone restarted the container. Any error now just
    costs 5 seconds, and the channel is re-resolved afterwards in case the
    cached object went stale.
    """
    r = aioredis.from_url(config.REDIS_URL)
    await client.wait_until_ready()
    channel = None
    try:
        while not client.is_closed():
            try:
                if channel is None:
                    channel = client.get_channel(config.NOTIFY_CHANNEL_ID) \
                        or await client.fetch_channel(config.NOTIFY_CHANNEL_ID)
                    logging.info("notifier: watching %s → #%s",
                                 config.EVENTS_KEY, channel)
                item = await r.brpop(config.EVENTS_KEY, timeout=30)
                if item is None:
                    continue
                await _announce(channel, json.loads(item[1]))
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("notifier: loop error, retrying in 5s")
                channel = None
                await asyncio.sleep(5)
    finally:
        with contextlib.suppress(Exception):
            await r.aclose()


async def _announce(channel, event):
    fname = event['filepath']
    url = f"{config.INDEX_PUBLIC_URL}/media/{fname}"
    ext = os.path.splitext(fname)[1].lstrip('.').lower()
    emoji, noun, colour = _TYPE_STYLE[_kind(ext)]
    uploader = event.get('uploaded_by') or 'Unknown'

    embed = discord.Embed(title=f"{emoji} New {noun}", url=url,
                          colour=discord.Colour(colour))
    # The author line links to the uploader's gallery (user:<name> search);
    # their user:* tag is dropped from the tag row for the same reason.
    embed.set_author(
        name=uploader,
        url=f"{config.INDEX_PUBLIC_URL}/?search="
            + urllib.parse.quote(f"user:{uploader}", safe=''))
    desc = [parsing.format_tag_links(event.get('tags'),
                                     config.INDEX_PUBLIC_URL)]

    # getattr on the channel too: a DM channel has no .guild at all, and an
    # AttributeError here would take out the whole announcement.
    limit = getattr(getattr(channel, 'guild', None), 'filesize_limit', None) \
        or config.DEFAULT_FILESIZE_LIMIT
    media_path = os.path.join(config.MEDIA_DIR, fname)
    size = os.path.getsize(media_path) if os.path.isfile(media_path) else None
    thumb = event.get('thumb')
    thumb_path = os.path.join(config.THUMB_DIR, thumb) if thumb else None
    have_thumb = bool(thumb_path and os.path.isfile(thumb_path))

    # Attach the original when it fits the server's upload cap; otherwise
    # fall back to the thumbnail + a note. NOTIFY_THUMBNAIL_ONLY forces
    # that fallback for everything, cap or no cap. Both come straight from
    # the read-only bind mounts — the web routes require a login. Kept as
    # (path, name) specs: a discord.File is single-use, so the retry loop
    # below must construct fresh ones per attempt.
    fits = size is not None and size <= limit
    attach = []
    if fits and not config.NOTIFY_THUMBNAIL_ONLY:
        attach.append((media_path, fname))
        if ext in config.IMAGE_EXT:
            embed.set_image(url=f'attachment://{fname}')
        elif have_thumb:
            # The video renders as its own player under the message; a
            # corner thumbnail keeps the embed itself from looking bare.
            attach.append((thumb_path, 'thumb.png'))
            embed.set_thumbnail(url='attachment://thumb.png')
    elif have_thumb:
        attach.append((thumb_path, 'thumb.png'))
        embed.set_image(url='attachment://thumb.png')
        # Only the size cap earns the apologetic note — a thumbnail posted
        # because the feed is configured that way isn't a fallback.
        note = "" if fits or size is None \
            else f"too large to attach ({_fmt_size(size)}) — "
        desc.append(f"*{note}[watch it in the index]({url})*")
    else:
        desc.append(f"*[view it in the index]({url})*")

    embed.description = '\n\n'.join(d for d in desc if d) or None
    if size is not None:
        label = noun if noun == 'GIF' else noun.capitalize()
        embed.set_footer(text=f"{label} · {_fmt_size(size)}")

    for attempt in range(3):
        try:
            files = [discord.File(p, filename=n) for p, n in attach]
            await channel.send(embed=embed, files=files)
            return
        except discord.HTTPException:
            logging.exception("notifier: send failed (attempt %d/3)", attempt + 1)
            await asyncio.sleep(2 ** attempt)
    logging.error("notifier: dropping event for %s after 3 attempts",
                  event.get('filepath'))
