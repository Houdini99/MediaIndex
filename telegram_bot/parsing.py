"""Caption parsing and human-readable formatting for the Telegram bot.

Pure stdlib so it stays unit-testable without any Telegram library
installed — same rule as discord_bot/parsing.py.

The link and tag-list parsing both bots do identically lives in
bots_common/parsing.py and is re-exported below.
"""
# Shared with the other bot — see bots_common/parsing.py. Re-exported so
# callers keep using `parsing.<name>`.
from bots_common.parsing import (  # noqa: F401
    extract_urls, format_progress, media_url_info, parse_message_tags,
    strip_urls,
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


def merge_tags(caption_tags, base_tags):
    """Final comma-separated tag string for one item.

    Caption tags first, then the configured base tags, deduped while
    keeping order — `dict.fromkeys` rather than a set, because tag order
    is what the user sees back in the gallery. The index normalizes the
    result again on the way in (app.media.db.normalize_tags), so this does
    not try to enforce the stored-format invariant itself.
    """
    return ','.join(dict.fromkeys(list(caption_tags) + list(base_tags)))


def split_base_tags(raw):
    """TELEGRAM_INGEST_TAGS env value → list of tags."""
    return [t.strip() for t in (raw or '').split(',') if t.strip()]


def file_ext(filename, fallback=''):
    """Lowercase extension of a filename, without the dot."""
    name = (filename or '').rsplit('/', 1)[-1]
    return name.rsplit('.', 1)[-1].lower() if '.' in name else fallback


def human_size(num_bytes):
    """Bytes → '12.3 MB', for size-limit messages."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def format_tag_list(tag_str):
    """Stored tag string → 'cats, funny' for a reply line."""
    return ', '.join(t.strip() for t in (tag_str or '').split(',') if t.strip())
