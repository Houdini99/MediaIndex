"""Caption parsing and human-readable formatting for the Telegram bot.

Pure stdlib so it stays unit-testable without any Telegram library
installed — same rule as discord_bot/parsing.py.

The link, tag and extension parsing both bots do identically lives in
bots_common/parsing.py and is re-exported below.
"""
# Shared with the other bot — see bots_common/parsing.py. Re-exported so
# callers keep using `parsing.<name>`.
from bots_common.parsing import (  # noqa: F401
    extract_urls, file_ext, format_progress, media_url_info, merge_tags,
    parse_message_tags, split_tags, strip_urls,
)


def group_caption(captions):
    """The one caption of an album, given every member's caption.

    Telegram puts the caption on a single item of a media group — usually
    the first, but that is not guaranteed and an album assembled by a
    client may put it anywhere. Returns the first non-empty one, or ''.
    """
    for caption in captions:
        if (caption or '').strip():
            return caption.strip()
    return ''


def human_size(num_bytes):
    """Bytes → '12.3 MB', for size-limit messages."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def format_tag_list(tag_str):
    """Stored tag string → 'cats, funny' for a reply line."""
    return ', '.join(split_tags(tag_str))
