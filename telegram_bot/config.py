"""Environment-driven configuration, mirroring the app's style and
discord_bot's: read at import time, fail fast on anything required
(bots_common/env.py).

A typo in a variable name should name itself at startup, not surface
three hours later as a bot that silently ingests nothing.
"""
import os

from bots_common import env, parsing

BOT_TOKEN = env.require('TELEGRAM_BOT_TOKEN')

# App password for the index's JSON API, generated on the index's /queue
# page. The account it belongs to must be listed in the web app's
# DELEGATED_UPLOADERS: this bot owns nothing itself, it uploads *on behalf
# of* the index user who linked the sending Telegram account, and the
# identity lookup endpoint is restricted to delegated accounts too.
INDEX_BOT_USERNAME = os.environ.get('INDEX_BOT_USERNAME', 'Bot')
INDEX_BOT_APP_PASSWORD = env.require('INDEX_BOT_APP_PASSWORD').strip()

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
ALBUM_WAIT = env.num('TELEGRAM_ALBUM_WAIT', 2.0)

# How long an item with no tags waits for them before being forgotten.
# Only the Telegram file_id is parked, never the bytes, so this costs a
# few hundred bytes of Redis per waiting item.
PENDING_TTL = env.num('TELEGRAM_PENDING_TTL', 86400, int)
# Cap on how many items one user may have waiting. A user who never
# answers the tag prompt must not be able to grow the store without
# bound; the oldest entry is dropped when this is exceeded.
PENDING_MAX = env.num('TELEGRAM_PENDING_MAX', 100, int)

# Identity lookups are cached this long, negative answers included, so a
# flood from one unlinked account costs one request per minute.
LOOKUP_TTL = env.num('TELEGRAM_LOOKUP_TTL', 60)
# An unlinked sender is told so at most once per this many seconds. Not
# silent — a linked user whose @username changed would otherwise face a
# bot that ignores them with no explanation — but not unthrottled either,
# because a stranger must not be able to make the bot chatty.
UNLINKED_NOTICE_EVERY = env.num('TELEGRAM_UNLINKED_NOTICE_EVERY', 3600)

# Backoff after a rejected app password: 10 failed logins in 15 minutes
# get the container's IP banned for 24h, so never retry hot.
AUTH_RETRY_SECONDS = env.num('AUTH_RETRY_SECONDS', 900)

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
MAX_DOWNLOAD_BYTES = int(env.num('TELEGRAM_MAX_DOWNLOAD_MB',
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
LINKS_ENABLED = env.flag('TELEGRAM_LINKS', True)
# Cap on a direct media-link download. Checked against Content-Length up
# front and against the running total during the transfer.
LINK_MAX_BYTES = int(env.num('TELEGRAM_LINK_MAX_MB', 512) * 1e6)
# yt-dlp resolver for platform links (YouTube, TikTok, …): the
# TELEGRAM_YTDLP* variables, read and documented in bots_common/env.py.
_ytdlp = env.ytdlp_settings('TELEGRAM')
YTDLP_ENABLED = _ytdlp.enabled
YTDLP_MAX_BYTES = _ytdlp.max_bytes
YTDLP_STALL_TIMEOUT = _ytdlp.stall_timeout
YTDLP_MAX_DURATION = _ytdlp.max_duration
YTDLP_COOKIES = _ytdlp.cookies
YTDLP_QUALITY = _ytdlp.quality
YTDLP_POT_PROVIDER = _ytdlp.pot_provider

# What the index accepts, so an unsupported file is refused here instead
# of after a wasted download and upload.
IMAGE_EXT = parsing.IMAGE_EXT
ALLOWED_EXT = parsing.ALLOWED_EXT

# Redis key prefixes (bot-owned; the web app never touches these).
PENDING_KEY = 'telegram:pending'
FLAG_KEY = 'telegram:flags'
NOTICE_KEY = 'telegram:notice'
