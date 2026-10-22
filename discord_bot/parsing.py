"""Message parsing and human-readable formatting for the bot.

Discord message text in (links, tag lists, link commands), strings for
logs and embeds out. Pure stdlib so it stays unit-testable without discord.py
installed. The link and tag-list parsing shared with the Telegram bot lives
in bots_common/parsing.py and is re-exported below.

Nothing here scrapes HTML: the bot uploads through /api/upload.
"""
import re
import urllib.parse

# Shared with the other bot — see bots_common/parsing.py. Re-exported so
# callers keep using `parsing.<name>`.
from bots_common.parsing import (  # noqa: F401
    extract_urls, file_ext, format_progress, media_url_info, merge_tags,
    parse_message_tags, split_tags, strip_urls,
)


# Discord serves attachments from these hosts behind short-lived signed
# query params (ex/is/hm); an expired link 404s and must be re-signed.
_DISCORD_CDN_HOSTS = frozenset(('cdn.discordapp.com', 'media.discordapp.net'))


def is_discord_cdn(url):
    """True for Discord's signed, expiring attachment CDN hosts."""
    try:
        return urllib.parse.urlsplit(url).hostname in _DISCORD_CDN_HOSTS
    except ValueError:
        return False


# `link K7QX-M2PA` — the account-link code from the index's /settings page.
# The command word may carry a ! or / prefix out of habit; the code may be
# typed without its dash. Validated properly by the index, not here.
_LINK_COMMAND = re.compile(r'\s*[!/]?link(?:\s+(\S+))?\s*', re.I)
_LINK_CODE = re.compile(r'[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}')


def parse_link_command(text):
    """The code from a link-command DM.

    None when the message is not a link command at all — including "link"
    followed by anything that is not shaped like a code, so a DM such as
    "link https://…" still reaches ingest. '' for a bare "link", which
    deserves an explanation rather than silence.
    """
    m = _LINK_COMMAND.fullmatch(text or '')
    if not m:
        return None
    code = m.group(1) or ''
    if code and not _LINK_CODE.fullmatch(code):
        return None
    return code


def format_duration(seconds):
    """Coarse human duration for a status line: '45s', '2m 30s', '1h 30m'.

    Rounds to the two largest units — this labels a retry delay, not a
    stopwatch.
    """
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def format_tag_links(tags, base_url, limit=1024):
    """Comma-separated tag string → Discord-markdown links into the
    gallery's tag search, ' · '-separated ('[cats](…/?search=cats) · …').

    user:* tags are dropped — announcements already name the uploader.
    Output stays under `limit` chars (a huge tag list must not blow the
    4096-char embed description); truncation appends an ellipsis.
    """
    parts, total = [], 0
    for tag in dict.fromkeys(split_tags(tags)):
        if tag.startswith('user:'):
            continue
        label = tag.replace('[', '').replace(']', '') or tag
        link = (f"[{label}]({base_url}/?search="
                f"{urllib.parse.quote(tag, safe='')})")
        if total + len(link) + 4 > limit:
            parts.append('…')
            break
        parts.append(link)
        total += len(link) + 3
    return ' · '.join(parts)
