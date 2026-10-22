"""Environment-driven configuration, mirroring the app's style: read at
import time, fail fast on anything required (bots_common/env.py)."""
import os

from bots_common import env, parsing

BOT_TOKEN = env.require('DISCORD_BOT_TOKEN')
NOTIFY_CHANNEL_ID = env.num('DISCORD_NOTIFY_CHANNEL_ID', cast=int)
INGEST_CHANNEL_ID = env.num('DISCORD_INGEST_CHANNEL_ID', cast=int)
# App password for the index's JSON upload API, generated on the index's
# /queue page. Uploads go to POST /api/upload with HTTP Basic auth — no
# CSRF scraping, no session cookie, and duplicates come back naming the
# item they collided with.
#
# Required: the bot has no other way to upload, so an unset app password
# fails at startup. The bot account's *login* password is never needed.
INDEX_BOT_APP_PASSWORD = env.require('INDEX_BOT_APP_PASSWORD').strip()

# Optional bot-logs channel: human-readable status lines (startup, per-file
# ingest results, tag matches, failures). Unset/empty disables posting.
LOG_CHANNEL_ID = env.num('DISCORD_LOG_CHANNEL_ID', 0, int)

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
LOOKUP_TTL = env.num('DISCORD_LOOKUP_TTL', 60)
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
DM_INGEST = env.flag('DISCORD_DM_INGEST', True)
# Forwards can't carry a comment, so their tag list arrives as a separate
# follow-up message. Untagged forwards wait this many seconds for it before
# ingesting (a matching tag message ingests immediately; 0 = don't wait).
FORWARD_TAG_WAIT = env.num('DISCORD_FORWARD_TAG_WAIT', 15)
# Discord unfurls link embeds asynchronously — a page link (Tenor, image
# host) may arrive before its embed exists. Re-fetch the message once after
# this many seconds when a link yielded no ingestible media yet.
EMBED_UNFURL_WAIT = 3.0
# Cap on a download from a link (in the text or behind an embed). Checked
# against Content-Length up front and against the running total during the
# transfer. Attachments on Discord's own CDN are bounded by Discord's
# upload limit instead.
LINK_MAX_BYTES = int(env.num('DISCORD_LINK_MAX_MB', 512) * 1e6)
# Backoff after a *failed* login: 10 failures within 15-minute windows get
# the container's IP banned for 24h, so never retry hot. IndexClient also
# enforces this itself, so concurrent ingests can't slip past the backoff
# in the window between a rejection and the bot noticing it.
AUTH_RETRY_SECONDS = env.num('AUTH_RETRY_SECONDS', 900)

# Ingests that fail for a *transient* reason (index login backing off, a
# network error, a 5xx from the source) are queued in Redis and retried
# with exponential backoff instead of being dropped. 'false' disables.
INGEST_RETRY = env.flag('DISCORD_INGEST_RETRY', True)
INGEST_RETRY_ATTEMPTS = env.num('DISCORD_INGEST_RETRY_ATTEMPTS', 5, int)
# How often the queue is drained; also the floor on how quickly a retry
# can happen once due.
INGEST_RETRY_POLL = env.num('DISCORD_INGEST_RETRY_POLL', 30)
# First retry delay; doubles per attempt up to INGEST_RETRY_MAX_DELAY.
INGEST_RETRY_DELAY = env.num('DISCORD_INGEST_RETRY_DELAY', 60)
INGEST_RETRY_MAX_DELAY = env.num('DISCORD_INGEST_RETRY_MAX_DELAY', 3600)
# yt-dlp resolver: platform links (YouTube, TikTok, Twitter, …) that carry
# no directly ingestible media are resolved to a real video file. The
# DISCORD_YTDLP* variables are read and documented in bots_common/env.py.
_ytdlp = env.ytdlp_settings('DISCORD')
YTDLP_ENABLED = _ytdlp.enabled
YTDLP_MAX_BYTES = _ytdlp.max_bytes
YTDLP_STALL_TIMEOUT = _ytdlp.stall_timeout
YTDLP_MAX_DURATION = _ytdlp.max_duration
YTDLP_COOKIES = _ytdlp.cookies
YTDLP_QUALITY = _ytdlp.quality
YTDLP_POT_PROVIDER = _ytdlp.pot_provider
# Links no real yt-dlp extractor claims may still have a video behind them
# that only the generic extractor finds. Each is probed — metadata only,
# public addresses only — before anything downloads, and only this many
# per message, so a link dump can't become a crawl. 0 disables.
YTDLP_MAX_PROBES = env.num('DISCORD_YTDLP_MAX_PROBES', 4, int)
YTDLP_PROBE_TIMEOUT = env.num('DISCORD_YTDLP_PROBE_TIMEOUT', 45)

# What the index accepts, so unsupported attachments are skipped without
# a wasted upload round-trip.
IMAGE_EXT = parsing.IMAGE_EXT
ALLOWED_EXT = parsing.ALLOWED_EXT

# Per-file upload cap when the guild's own limit is unavailable (DMs).
DEFAULT_FILESIZE_LIMIT = 10 * 1024 * 1024
# Announcements normally attach the original file whenever it fits the
# cap above, and only fall back to the thumbnail when it doesn't. 'true'
# here always posts the thumbnail instead — same embed, no full-size
# upload — for feeds that should stay light. Items with no thumbnail on
# disk announce as a bare link either way.
NOTIFY_THUMBNAIL_ONLY = env.flag('DISCORD_NOTIFY_THUMBNAIL_ONLY', False)

# Must match app/notifications.py.
EVENTS_KEY = 'discord:media_events'
# Retry queue (bot-owned; the web app never touches it).
RETRY_KEY = 'discord:ingest_retry'
