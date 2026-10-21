"""Environment-driven configuration, mirroring the app's style: read at
import time, fail fast on anything required."""
import os


def _require(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value


def _num(name, default, cast=float):
    """Numeric env var with the same fail-fast manners as _require: a typo
    names itself instead of surfacing as a bare ValueError traceback."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return cast(default)
    try:
        return cast(raw)
    except ValueError:
        raise RuntimeError(
            f"{name} must be a number, got {raw!r}") from None


BOT_TOKEN = _require('DISCORD_BOT_TOKEN')
NOTIFY_CHANNEL_ID = _num('DISCORD_NOTIFY_CHANNEL_ID',
                         _require('DISCORD_NOTIFY_CHANNEL_ID'), int)
INGEST_CHANNEL_ID = _num('DISCORD_INGEST_CHANNEL_ID',
                         _require('DISCORD_INGEST_CHANNEL_ID'), int)
# App password for the index's JSON upload API, generated on the index's
# /queue page. Uploads go to POST /api/upload with HTTP Basic auth — no
# CSRF scraping, no session cookie, and duplicates come back naming the
# item they collided with.
#
# Required: the bot has no other way to upload, so an unset app password
# fails at startup. The bot account's *login* password is never needed.
INDEX_BOT_APP_PASSWORD = _require('INDEX_BOT_APP_PASSWORD').strip()

# Optional bot-logs channel: human-readable status lines (startup, per-file
# ingest results, tag matches, failures). Unset/empty disables posting.
_log_channel = os.environ.get('DISCORD_LOG_CHANNEL_ID', '').strip()
LOG_CHANNEL_ID = int(_log_channel) if _log_channel.isdigit() else 0

INDEX_INTERNAL_URL = os.environ.get('INDEX_INTERNAL_URL', 'http://web:5001').rstrip('/')
INDEX_PUBLIC_URL = os.environ.get('INDEX_PUBLIC_URL', 'https://media.example.com').rstrip('/')
INDEX_BOT_USERNAME = os.environ.get('INDEX_BOT_USERNAME', 'Bot')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379')
THUMB_DIR = os.environ.get('THUMB_DIR', '/thumbnails')
MEDIA_DIR = os.environ.get('MEDIA_DIR', '/media_files')
INGEST_TAGS = os.environ.get('DISCORD_INGEST_TAGS', 'discord')
# Who owns an ingested item is the index's answer: users link their Discord
# account through the index's /settings page (a code they DM the bot, see
# IndexBot._link_account), and the bot asks GET
# /api/discord/user (only DELEGATED_UPLOADERS accounts may). Unlinked
# authors' channel posts stay owned by the bot account. Answers are cached
# this many seconds, "not linked" included, so a message with ten
# attachments costs one request.
LOOKUP_TTL = _num('DISCORD_LOOKUP_TTL', 60)
# Discord role required before a message's media is ingested at all.
#
# This is the gate on the *only* route into the media pipeline that needs no
# index account: anyone able to post in the ingest channel otherwise reaches
# yt-dlp here and ffmpeg in the web container with arbitrary input, and the
# web container runs as root. The account link is unrelated — it
# decides *who owns* an ingested item, not *whether* it is ingested.
#
# Accepts a role id (preferred — survives a rename, and Discord permits
# duplicate role names) or a role name. Empty disables the gate, which is
# warned about at startup rather than failing, so an existing deployment
# does not lose ingest the moment it redeploys without the variable.
INGEST_ROLE = os.environ.get('DISCORD_INGEST_ROLE', '').strip()


def author_is_verified(author):
    """True when `author` is allowed to have their media ingested.

    Guild messages carry a partial member object in the gateway payload, so
    discord.py builds a real `Member` and `.roles` is populated *without*
    the privileged `members` intent — no Developer Portal change needed.

    Anything without `.roles` (a `User` from a DM, a webhook author) is
    refused while the gate is on: the check fails closed, since the whole
    point is to keep unattributable posters out of the decoder.
    """
    if not INGEST_ROLE:
        return True
    roles = getattr(author, 'roles', None) or ()
    if INGEST_ROLE.isdigit():
        wanted = int(INGEST_ROLE)
        return any(getattr(r, 'id', None) == wanted for r in roles)
    return any(getattr(r, 'name', None) == INGEST_ROLE for r in roles)


# Members holding INGEST_ROLE may also DM the bot: media sent there is
# indexed *privately* — encrypted under the sender's own index account,
# visible to nobody else, never announced. A DM carries a User, not a
# Member, so the role is looked up in the ingest channel's server. Needs
# INGEST_ROLE (with the gate off, DMs are refused rather than opened to
# anyone sharing a server with the bot) and a linked index account for
# the sender: a private item has to belong to someone, and it must never
# be the bot account. 'false' disables.
DM_INGEST = os.environ.get('DISCORD_DM_INGEST', 'true') \
    .strip().lower() != 'false'
# Forwards can't carry a comment, so their tag list arrives as a separate
# follow-up message. Untagged forwards wait this many seconds for it before
# ingesting (a matching tag message ingests immediately; 0 = don't wait).
FORWARD_TAG_WAIT = _num('DISCORD_FORWARD_TAG_WAIT', 15)
# Discord unfurls link embeds asynchronously — a page link (Tenor, image
# host) may arrive before its embed exists. Re-fetch the message once after
# this many seconds when a link yielded no ingestible media yet.
EMBED_UNFURL_WAIT = 3.0
# Backoff after a *failed* login: 10 failures within 15-minute windows get
# the container's IP banned for 24h, so never retry hot. IndexClient also
# enforces this itself, so concurrent ingests can't slip past the backoff
# in the window between a rejection and the bot noticing it.
AUTH_RETRY_SECONDS = _num('AUTH_RETRY_SECONDS', 900)

# Ingests that fail for a *transient* reason (index login backing off, a
# network error, a 5xx from the source) are queued in Redis and retried
# with exponential backoff instead of being dropped. 'false' disables.
INGEST_RETRY = os.environ.get('DISCORD_INGEST_RETRY', 'true') \
    .strip().lower() != 'false'
INGEST_RETRY_ATTEMPTS = _num('DISCORD_INGEST_RETRY_ATTEMPTS', 5, int)
# How often the queue is drained; also the floor on how quickly a retry
# can happen once due.
INGEST_RETRY_POLL = _num('DISCORD_INGEST_RETRY_POLL', 30)
# First retry delay; doubles per attempt up to INGEST_RETRY_MAX_DELAY.
INGEST_RETRY_DELAY = _num('DISCORD_INGEST_RETRY_DELAY', 60)
INGEST_RETRY_MAX_DELAY = _num('DISCORD_INGEST_RETRY_MAX_DELAY', 3600)
# yt-dlp resolver: platform links (YouTube, TikTok, Twitter, …) that carry
# no directly ingestible media are resolved to a real video file. On by
# default; 'false' disables (a missing yt-dlp module disables it too).
YTDLP_ENABLED = os.environ.get('DISCORD_YTDLP', 'true').strip().lower() != 'false'
YTDLP_MAX_BYTES = int(_num('DISCORD_YTDLP_MAX_MB', 512) * 1e6)
# A download is aborted only when it *stalls* — no new bytes for this many
# seconds. It watches progress, not total runtime, so a large-but-healthy
# download is never cut off (a 1 GB file at 3 MB/s is fine; a frozen one
# dies). The abort happens inside yt-dlp, so the transfer actually stops.
YTDLP_STALL_TIMEOUT = _num('DISCORD_YTDLP_STALL_TIMEOUT', 120)
# Absolute backstop for a resolve that hangs *before* the download starts
# (extraction, where the stall watchdog can't fire yet). Generous on
# purpose — a safety net, not the primary limit.
YTDLP_MAX_DURATION = _num('DISCORD_YTDLP_MAX_DURATION', 1800)
# Logins for sites that hide videos behind one (YouTube, Instagram, X,
# Facebook, Reddit, …) live in the index: an admin uploads a cookies.txt
# per site on /admin/site-logins, and the bot fetches the one a link needs
# before resolving it (bots_common/site_logins.py). This optional mounted
# Netscape cookies.txt is only the fallback for a link whose site has no
# login stored there. Empty = none.
YTDLP_COOKIES = os.environ.get('DISCORD_YTDLP_COOKIES', '').strip()
# Resolution cap — a preference, not a filter (see resolver._format_sort),
# with H.264 preferred at any height. 1080, not 'best': the index takes
# 512 MB and this bot indexes clips, not archival masters, and a 4K pull
# is mostly VP9/AV1 that blows the cap anyway.
YTDLP_QUALITY = os.environ.get('DISCORD_YTDLP_QUALITY', '1080').strip()
if YTDLP_QUALITY not in ('best', '2160', '1440', '1080', '720', '480'):
    raise RuntimeError(
        f"DISCORD_YTDLP_QUALITY must be one of best/2160/1440/1080/720/480, "
        f"got {YTDLP_QUALITY!r}")
# bgutil PO-token server (the `bgutil` sidecar in the stack). It answers
# YouTube's "Sign in to confirm you're not a bot" for a server IP, so most
# public videos need no login at all. Empty disables it.
YTDLP_POT_PROVIDER = os.environ.get('DISCORD_YTDLP_POT_PROVIDER', '') \
    .strip().rstrip('/')
# Links no real yt-dlp extractor claims may still have a video behind them
# that only the generic extractor finds. Each is probed — metadata only,
# public addresses only — before anything downloads, and only this many
# per message, so a link dump can't become a crawl. 0 disables.
YTDLP_MAX_PROBES = _num('DISCORD_YTDLP_MAX_PROBES', 4, int)
YTDLP_PROBE_TIMEOUT = _num('DISCORD_YTDLP_PROBE_TIMEOUT', 45)

# Must mirror the app's allow-lists (app/config.py) so unsupported
# attachments are skipped without a wasted upload round-trip.
IMAGE_EXT = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
ALLOWED_EXT = IMAGE_EXT | {'mp4', 'webm', 'avi', 'mov', 'mkv'}

# Per-file upload cap when the guild's own limit is unavailable (DMs).
DEFAULT_FILESIZE_LIMIT = 10 * 1024 * 1024
# Announcements normally attach the original file whenever it fits the
# cap above, and only fall back to the thumbnail when it doesn't. 'true'
# here always posts the thumbnail instead — same embed, no full-size
# upload — for feeds that should stay light. Items with no thumbnail on
# disk announce as a bare link either way.
NOTIFY_THUMBNAIL_ONLY = os.environ.get(
    'DISCORD_NOTIFY_THUMBNAIL_ONLY', '').strip().lower() == 'true'

# Must match app/notifications.py.
EVENTS_KEY = 'discord:media_events'
# Retry queue (bot-owned; the web app never touches it).
RETRY_KEY = 'discord:ingest_retry'
