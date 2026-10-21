"""Environment-driven configuration, mirroring the app's style and
discord_bot's: read at import time, fail fast on anything required.

A typo in a variable name should name itself at startup, not surface
three hours later as a bot that silently ingests nothing.
"""
import os


def _require(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value


def _num(name, default, cast=float):
    """Numeric env var with the same fail-fast manners as _require."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return cast(default)
    try:
        return cast(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None


BOT_TOKEN = _require('TELEGRAM_BOT_TOKEN')

# App password for the index's JSON API, generated on the index's /queue
# page. The account it belongs to must be listed in the web app's
# DELEGATED_UPLOADERS: this bot owns nothing itself, it uploads *on behalf
# of* the index user who linked the sending Telegram account, and the
# identity lookup endpoint is restricted to delegated accounts too.
INDEX_BOT_USERNAME = os.environ.get('INDEX_BOT_USERNAME', 'Bot')
INDEX_BOT_APP_PASSWORD = _require('INDEX_BOT_APP_PASSWORD').strip()

INDEX_INTERNAL_URL = os.environ.get('INDEX_INTERNAL_URL', 'http://web:5001').rstrip('/')
INDEX_PUBLIC_URL = os.environ.get('INDEX_PUBLIC_URL', 'https://media.example.com').rstrip('/')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379')

# Tags added to every ingest on top of the caption's.
INGEST_TAGS = os.environ.get('TELEGRAM_INGEST_TAGS', 'telegram')

# Telegram delivers an album as N separate updates sharing a
# media_group_id, and only ONE of them carries the caption. Items are
# buffered for this long — each new member of the group restarting the
# clock — so the caption can be applied to the whole album. Without it a
# ten-photo album would ingest one tagged photo and hold nine for tags
# that were already sent.
ALBUM_WAIT = _num('TELEGRAM_ALBUM_WAIT', 2.0)

# How long an item with no tags waits for them before being forgotten.
# Only the Telegram file_id is parked, never the bytes, so this costs a
# few hundred bytes of Redis per waiting item.
PENDING_TTL = _num('TELEGRAM_PENDING_TTL', 86400, int)
# Cap on how many items one user may have waiting. A user who never
# answers the tag prompt must not be able to grow the store without
# bound; the oldest entry is dropped when this is exceeded.
PENDING_MAX = _num('TELEGRAM_PENDING_MAX', 100, int)

# Identity lookups are cached this long, negative answers included, so a
# flood from one unlinked account costs one request per minute.
LOOKUP_TTL = _num('TELEGRAM_LOOKUP_TTL', 60)
# An unlinked sender is told so at most once per this many seconds. Not
# silent — a linked user whose @username changed would otherwise face a
# bot that ignores them with no explanation — but not unthrottled either,
# because a stranger must not be able to make the bot chatty.
UNLINKED_NOTICE_EVERY = _num('TELEGRAM_UNLINKED_NOTICE_EVERY', 3600)

# Backoff after a rejected app password: 10 failed logins in 15 minutes
# get the container's IP banned for 24h, so never retry hot.
AUTH_RETRY_SECONDS = _num('AUTH_RETRY_SECONDS', 900)

# Optional local Bot API server (github.com/tdlib/telegram-bot-api). Empty
# uses api.telegram.org, where getFile refuses anything over 20 MB — a
# hard cap on what this bot can ingest, while the web form and WebDAV have
# none. A local server raises that to 2000 MB. Set to the server's base
# URL, e.g. 'http://telegram-bot-api:8081/bot'.
API_BASE = os.environ.get('TELEGRAM_API_BASE', '').strip().rstrip('/')
LOCAL_MODE = bool(API_BASE)
# The server's --dir, which MUST be mounted at this same path in both
# containers. In local mode getFile answers with a path on the server's
# filesystem, and python-telegram-bot decides whether to treat it as a
# local file by stat-ing it (telegram._utils.files.is_local_file) — so a
# work dir that is missing here does not raise, it silently degrades to
# treating the path as a URL. Checked at startup for that reason.
API_WORK_DIR = os.environ.get('TELEGRAM_API_WORK_DIR',
                              '/var/lib/telegram-bot-api').rstrip('/')
# Download ceiling enforced before we ask Telegram for the file, so an
# oversized item gets a clear answer instead of an opaque API error.
MAX_DOWNLOAD_BYTES = int(_num('TELEGRAM_MAX_DOWNLOAD_MB',
                              2000 if LOCAL_MODE else 20) * 1024 * 1024)

# ── Links ──────────────────────────────────────────────────────────────
# Send the bot a link and it downloads what the link points at. Two very
# different paths behind one feature, and only the first is a plain fetch:
#
#   * a URL that already names a media file ('…/cat.gif') is streamed
#     straight to disk by download.py, capped at TELEGRAM_LINK_MAX_MB;
#   * anything else is offered to yt-dlp (resolver.py), which downloads it
#     only when a real, non-generic extractor claims the URL.
#
# Both are attacker-influenced fetches made from inside the stack's
# networks, which is why the sender has to be a *linked index user* before
# either runs — the same gate that guards every other ingest, and the
# reason this is safe to have at all.
LINKS_ENABLED = os.environ.get('TELEGRAM_LINKS', 'true') \
    .strip().lower() != 'false'
# Cap on a direct media-link download. Checked against Content-Length up
# front and against the running total during the transfer.
LINK_MAX_BYTES = int(_num('TELEGRAM_LINK_MAX_MB', 512) * 1e6)
# yt-dlp resolver for platform links (YouTube, TikTok, …). On by default;
# 'false' disables (a missing yt-dlp module disables it too).
YTDLP_ENABLED = os.environ.get('TELEGRAM_YTDLP', 'true') \
    .strip().lower() != 'false'
YTDLP_MAX_BYTES = int(_num('TELEGRAM_YTDLP_MAX_MB', 512) * 1e6)
# A download is aborted only when it *stalls* — no new bytes for this many
# seconds. It watches progress, not total runtime, so a large-but-healthy
# download is never cut off (a 1 GB file at 3 MB/s is fine; a frozen one
# dies). The abort happens inside yt-dlp, so the transfer actually stops.
YTDLP_STALL_TIMEOUT = _num('TELEGRAM_YTDLP_STALL_TIMEOUT', 120)
# Absolute backstop for a resolve that hangs *before* the download starts
# (extraction, where the stall watchdog can't fire yet). Generous on
# purpose — a safety net, not the primary limit.
YTDLP_MAX_DURATION = _num('TELEGRAM_YTDLP_MAX_DURATION', 1800)
# Logins for sites that hide videos behind one (YouTube, Instagram, X,
# Facebook, Reddit, …) live in the index: an admin uploads a cookies.txt
# per site on /admin/site-logins, and the bot fetches the one a link needs
# before resolving it (bots_common/site_logins.py). This optional mounted
# Netscape cookies.txt is only the fallback for a link whose site has no
# login stored there. Empty = none.
YTDLP_COOKIES = os.environ.get('TELEGRAM_YTDLP_COOKIES', '').strip()
# Resolution cap, with H.264 preferred at any height — a preference, not a
# filter (see resolver._format_sort). 1080 keeps a platform video inside
# the 512 MB cap and playable in every browser.
YTDLP_QUALITY = os.environ.get('TELEGRAM_YTDLP_QUALITY', '1080').strip()
if YTDLP_QUALITY not in ('best', '2160', '1440', '1080', '720', '480'):
    raise RuntimeError(
        f"TELEGRAM_YTDLP_QUALITY must be one of best/2160/1440/1080/720/480, "
        f"got {YTDLP_QUALITY!r}")
# bgutil PO-token server (the `bgutil` sidecar in the stack): answers
# YouTube's "Sign in to confirm you're not a bot" for a server IP. Empty
# disables it.
YTDLP_POT_PROVIDER = os.environ.get('TELEGRAM_YTDLP_POT_PROVIDER', '') \
    .strip().rstrip('/')

# Must mirror the app's allow-lists (app/config.py) so an unsupported file
# is refused here instead of after a wasted download and upload.
IMAGE_EXT = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
ALLOWED_EXT = IMAGE_EXT | {'mp4', 'webm', 'avi', 'mov', 'mkv'}

# Redis key prefixes (bot-owned; the web app never touches these).
PENDING_KEY = 'telegram:pending'
FLAG_KEY = 'telegram:flags'
NOTICE_KEY = 'telegram:notice'
