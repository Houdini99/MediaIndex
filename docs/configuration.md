# Configuration

Every setting MediaIndex reads from the environment, grouped by the service
that reads it. This is the only page that lists variables; the other pages
link here.

**Contents**

- [How settings reach the containers](#how-settings-reach-the-containers)
- [Web app (`web`)](#web-app-web)
- [Database and Redis](#database-and-redis)
- [Discord bot (`discord-bot`)](#discord-bot-discord-bot)
- [Telegram bot (`telegram-bot`)](#telegram-bot-telegram-bot)
- [Telegram Bot API server (`telegram-bot-api`)](#telegram-bot-api-server-telegram-bot-api)
- [Build arguments](#build-arguments)
- [Settings in the code](#settings-in-the-code)

## How settings reach the containers

[`docker-compose.yml.example`](../docker-compose.yml.example) passes each
variable into its service as `${NAME:-default}`, and `docker compose`
fills those in from a `.env` file next to the compose file (copy
[`.env.example`](../.env.example)). Under Portainer there is no `.env`: set
the same names under the stack's **Environment variables**.

- **Required** means the stack, or the one container, will not start
  without it.
- A variable marked *not in the compose file* is read by the code but not
  passed through by the example compose file. To change it, add it to that
  service's `environment:` block, or to a `docker-compose.override.yml`.
- Booleans are `true` or `false`.

Variables the compose file marks with `:?` (`SECRET_KEY`, `DATABASE_URL`,
`REDIS_PASSWORD`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and the Discord bot's
`DISCORD_BOT_TOKEN`, `DISCORD_NOTIFY_CHANNEL_ID`, `DISCORD_INGEST_CHANNEL_ID`
and `DISCORD_INGEST_ROLE`) stop the *whole* stack from deploying when they
are missing. The Telegram services' variables are deliberately not marked
that way: unset, only those containers crash-loop, each naming the
variable it wants, while the rest of the stack runs on. If you don't use
the Discord bot, delete the `discord-bot` service so its variables stop
being required.

## Web app (`web`)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | **yes** | — | Signs sessions, and is the key the stored [site logins](link-downloads.md#site-logins) are sealed under. Generate one with `python3 -c "import secrets; print(secrets.token_hex(32))"`. Changing it logs everyone out and makes the stored site logins unreadable until they are uploaded again. |
| `DATABASE_URL` | **yes** | — | PostgreSQL DSN, e.g. `postgresql://user:password@postgres:5432/mediaindex`. Must match `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB`. |
| `REDIS_URL` | no | `redis://localhost:6379` | Rate-limit storage, WebDAV locks and the Discord announcement queue. The compose file builds it from `REDIS_PASSWORD`; set it yourself only for a Redis outside the stack. |
| `PORT` | no | `5001` | Host port the compose file publishes the app on, on loopback only. Inside the container gunicorn always listens on 5001. For local development, the port `run.py` serves on. |
| `DEV_MODE` | no | `false` | `true` drops the `Secure` flag from the session cookie, for local development over plain HTTP. Never set it in production. |
| `SESSION_DAYS` | no | `30` | Days a login lasts without a visit. The window restarts on every request, and it is also the longest a captured session cookie stays valid. |
| `BAN_DURATION_HOURS` | no | `24` | How long an automatic failed-login ban lasts. Bans set by an admin never expire. |
| `TRUSTED_PROXIES` | no | loopback and private ranges | Comma-separated IPs or CIDRs whose `X-Forwarded-*` headers are believed. Set it to your reverse proxy's fixed address: the default trusts every container on a shared proxy network. See [Security](security.md#reverse-proxy-and-client-addresses). |
| `REGISTRATION_OPEN` | no | `false` | `true` opens the `/register` page to anyone who can reach it. Leave it closed: MediaIndex is for a small private group, and admins create accounts on the admin panel. Open it only to create the first account; see [Installation](installation.md#create-the-first-admin). |
| `AI_TAGS` | no | `fake,ai,grok,deepfake,gemini,chatgpt` | Comma-separated tags the *Hide AI* filter hides. Empty keeps the default list. |
| `DELEGATED_UPLOADERS` | no | `Bot` | Comma-separated usernames allowed to upload on another user's behalf, and the only accounts the bot-only endpoints answer. Both bots' index account must be listed. |
| `TELEGRAM_BOT_NAME` | no | — | The Telegram bot's @handle, shown on `/settings` so users know whom to message. Display only. |
| `DAV_ENABLED` | no | `true` | Serve the [WebDAV inbox](webdav.md) at `/dav`. |
| `STAGING_SCAN_INTERVAL` | no | `5` | Seconds between scans of the WebDAV inbox. |
| `STAGING_STABLE_AGE` | no | `10` | A staged file whose last change is older than this many seconds is picked up at once instead of waiting a scan cycle to see that it stopped growing. |
| `DUPE_ITEM_TIMEOUT` | no | `180` | Seconds one video may take in the [duplicate scan](admin.md#duplicate-finder) before its decoder is killed and the item recorded as an error. |
| `VIDEO_THUMB_TIMEOUT` | no | `180` | Seconds one video thumbnail may take to render before its decoder is killed and the placeholder is kept. |
| `STAGING_SCANNER` | no | `true` | `false` turns the WebDAV inbox scanner off. Meant for tests; *not in the compose file*. |
| `INDEX_ROOT` | no | `.` | Host directory the compose file's bind mounts resolve against. Under Portainer, set the absolute path of the checkout. Read by `docker compose`, not by the app. |

## Database and Redis

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `POSTGRES_USER` | **yes** | — | User the `postgres` service is created with; the backup sidecar logs in with it too. |
| `POSTGRES_PASSWORD` | **yes** | — | That user's password. Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`. |
| `POSTGRES_DB` | no | `mediaindex` | Database name. |
| `REDIS_PASSWORD` | **yes** | — | Redis `requirepass`. Every service's `REDIS_URL` is built from it. Keep it alphanumeric: an `@`, `:` or `/` breaks the URL parsing behind the WebDAV lock storage. Generate one with `python3 -c "import secrets; print(secrets.token_hex(24))"`. |

The database, the PostgreSQL version policy and the backup sidecar are
covered in [Operations](operations.md).

## Discord bot (`discord-bot`)

See [Discord bot](discord-bot.md) for what each feature does, and
[Link downloads](link-downloads.md) for the yt-dlp settings.

**Connection and identity**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | **yes** | — | The bot's token from the Discord Developer Portal. |
| `DISCORD_NOTIFY_CHANNEL_ID` | **yes** | — | Channel new public media is announced in. |
| `DISCORD_INGEST_CHANNEL_ID` | **yes** | — | Channel watched for media to index. Its server is also where DM senders' roles are checked. |
| `DISCORD_INGEST_ROLE` | **yes** | — | Role a member must hold before anything they post is indexed. Prefer the numeric role ID: a name matches whichever role carries it, after a rename or when two roles share it. Outside the compose file an empty value turns the check off, with a warning at startup. |
| `INDEX_BOT_APP_PASSWORD` | **yes** | — | App password of the bot's index account, generated on `/queue` while logged in as that account. |
| `INDEX_BOT_USERNAME` | no | `Bot` | The bot's index account. It must exist and be listed in `DELEGATED_UPLOADERS`. |
| `INDEX_INTERNAL_URL` | no | `http://web:5001` | Where the bot reaches the web app inside the stack. |
| `INDEX_PUBLIC_URL` | no | `https://media.example.com` | Public address of your index, used for the links in announcements. |
| `AUTH_RETRY_SECONDS` | no | `900` | How long the bot stops calling the index after its app password is refused, so it never earns its container an IP ban. |

**Ingest**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_INGEST_TAGS` | no | `discord` | Tags added to everything the bot indexes. |
| `DISCORD_DM_INGEST` | no | `true` | Role holders may DM the bot, and what they send is indexed as private media of their linked account. Needs `DISCORD_INGEST_ROLE`. |
| `DISCORD_LOOKUP_TTL` | no | `60` | Seconds the bot caches which index account a Discord user is linked to. |
| `DISCORD_FORWARD_TAG_WAIT` | no | `15` | Seconds an untagged forward waits for a follow-up message with its tags. `0` doesn't wait. |
| `DISCORD_LINK_MAX_MB` | no | `512` | Size limit for a file downloaded from a link or an embed, in MB. Attachments are limited by Discord's own upload limit instead. |
| `DISCORD_LOG_CHANNEL_ID` | no | — | Channel for status lines (startup, each ingest result, failures). Unset turns it off. |
| `DISCORD_NOTIFY_THUMBNAIL_ONLY` | no | `false` | `true` always attaches the thumbnail to an announcement, never the original file. |

**Retries**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_INGEST_RETRY` | no | `true` | Queue ingests that failed for a temporary reason and try them again. |
| `DISCORD_INGEST_RETRY_ATTEMPTS` | no | `5` | Retries before giving up. |
| `DISCORD_INGEST_RETRY_DELAY` | no | `60` | First retry delay in seconds; it doubles with each attempt. |
| `DISCORD_INGEST_RETRY_MAX_DELAY` | no | `3600` | Longest delay between retries, in seconds. |
| `DISCORD_INGEST_RETRY_POLL` | no | `30` | Seconds between checks of the retry queue. |

**Link downloads (yt-dlp)**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_YTDLP` | no | `true` | Download links to video sites with yt-dlp. |
| `DISCORD_YTDLP_MAX_MB` | no | `512` | Size limit for one downloaded video, in MB. |
| `DISCORD_YTDLP_QUALITY` | no | `1080` | Highest resolution to pick: `best`, `2160`, `1440`, `1080`, `720` or `480`. H.264 is preferred at every height. |
| `DISCORD_YTDLP_STALL_TIMEOUT` | no | `120` | Abort a download after this many seconds without progress. |
| `DISCORD_YTDLP_MAX_DURATION` | no | `1800` | Abort a link that is still not downloading after this many seconds. |
| `DISCORD_YTDLP_POT_PROVIDER` | no | `http://bgutil:4416` in the compose file, empty in the code | Address of the [`bgutil`](link-downloads.md#youtube-po-tokens) PO-token server. Empty turns it off. |
| `DISCORD_YTDLP_COOKIES` | no | — | In-container path of a `cookies.txt` used for sites with no stored login. You mount the file yourself. |
| `DISCORD_YTDLP_MAX_PROBES` | no | `4` | Links per message that no yt-dlp site claims and that are checked for a video anyway. `0` turns this off. |
| `DISCORD_YTDLP_PROBE_TIMEOUT` | no | `45` | Seconds one such check may take. |

**Paths inside the container** (*not in the compose file*; the defaults
match the compose file's mounts)

| Variable | Default | Purpose |
|---|---|---|
| `THUMB_DIR` | `/thumbnails` | Where the read-only thumbnail mount is. |
| `MEDIA_DIR` | `/media_files` | Where the read-only media mount is. |

## Telegram bot (`telegram-bot`)

See [Telegram bot](telegram-bot.md) and [Link downloads](link-downloads.md).

**Connection and identity**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | — | Token from [@BotFather](https://t.me/BotFather). |
| `INDEX_BOT_APP_PASSWORD` | **yes** | — | App password of the bot's index account (the same account and password the Discord bot can use). |
| `INDEX_BOT_USERNAME` | no | `Bot` | The bot's index account. It must be listed in `DELEGATED_UPLOADERS`. |
| `INDEX_INTERNAL_URL` | no | `http://web:5001` | Where the bot reaches the web app. |
| `INDEX_PUBLIC_URL` | no | `https://media.example.com` | Public address of your index, used in replies. |
| `AUTH_RETRY_SECONDS` | no | `900` | How long the bot stops calling the index after its app password is refused. |
| `TELEGRAM_API_BASE` | no | `http://telegram-bot-api:8081` in the compose file, empty in the code | Bot API server to use. Empty means `api.telegram.org`, which limits files to 20 MB. Because the compose file only applies its default when the variable is unset, set it to an empty value to switch to the cloud API. |
| `TELEGRAM_API_WORK_DIR` | no | `/var/lib/telegram-bot-api` | Where the Bot API server's work directory is mounted. It must be the same path in both containers. |
| `TELEGRAM_MAX_DOWNLOAD_MB` | no | `2000` with a local server, `20` without | Largest file the bot asks Telegram for. *Not in the compose file.* |

**Ingest**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_INGEST_TAGS` | no | `telegram` | Tags added to everything the bot indexes. |
| `TELEGRAM_ALBUM_WAIT` | no | `2` | Seconds the bot collects an album's items before applying its caption to all of them. |
| `TELEGRAM_PENDING_TTL` | no | `86400` | Seconds media sent without tags waits for them before it is forgotten. |
| `TELEGRAM_PENDING_MAX` | no | `100` | Items one user may have waiting for tags; the oldest is dropped beyond that. |
| `TELEGRAM_UNLINKED_NOTICE_EVERY` | no | `3600` | An unlinked sender is told why nothing happens at most once per this many seconds. |
| `TELEGRAM_LOOKUP_TTL` | no | `60` | Seconds the bot caches which index account a Telegram user is linked to. *Not in the compose file.* |

**Links**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_LINKS` | no | `true` | Download links sent to the bot. `false` ignores links. |
| `TELEGRAM_LINK_MAX_MB` | no | `512` | Size limit for a link that points straight at a file, in MB. |
| `TELEGRAM_YTDLP` | no | `true` | Download links to video sites with yt-dlp. `false` keeps direct file links working. |
| `TELEGRAM_YTDLP_MAX_MB` | no | `512` | Size limit for one downloaded video, in MB. |
| `TELEGRAM_YTDLP_QUALITY` | no | `1080` | Highest resolution: `best`, `2160`, `1440`, `1080`, `720` or `480`. |
| `TELEGRAM_YTDLP_STALL_TIMEOUT` | no | `120` | Abort a download after this many seconds without progress. |
| `TELEGRAM_YTDLP_MAX_DURATION` | no | `1800` | Abort a link that is still not downloading after this many seconds. |
| `TELEGRAM_YTDLP_POT_PROVIDER` | no | `http://bgutil:4416` in the compose file, empty in the code | Address of the `bgutil` PO-token server. Empty turns it off. |
| `TELEGRAM_YTDLP_COOKIES` | no | — | In-container path of a `cookies.txt` for sites with no stored login. You mount the file yourself. |

## Telegram Bot API server (`telegram-bot-api`)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_API_ID` | **yes** | — | api_id from [my.telegram.org](https://my.telegram.org) → *API development tools*. It identifies the server, not the bot. |
| `TELEGRAM_API_HASH` | **yes** | — | The matching api_hash. |

The compose file also sets `TELEGRAM_LOCAL=1` and `TELEGRAM_WORK_DIR`;
leave those as they are.

## Build arguments

| Argument | Image | Purpose |
|---|---|---|
| `YTDLP_REFRESH` | both bots | Any new value makes the build install the newest yt-dlp instead of reusing a cached layer. `./build.sh` passes the current time. |
| `DENO_VERSION`, `DENO_SHA256_X86_64`, `DENO_SHA256_AARCH64` | both bots | The pinned deno release yt-dlp uses for YouTube, and its checksums. To upgrade, change all three; the sums are in the release's `deno-<arch>-unknown-linux-gnu.zip.sha256sum` files. |

## Settings in the code

These are constants in [`app/config.py`](../app/config.py), not variables.
Changing one means editing the file and rebuilding the image.

| Setting | Value |
|---|---|
| Image types | `jpg`, `jpeg`, `png`, `webp` |
| GIF | `gif` |
| Video types | `mp4`, `webm`, `avi`, `mov`, `mkv` (AVI, MOV and MKV may not play in every browser) |
| Largest upload | 10 GiB per request |
| Thumbnail size | 150 × 150 |
| Gallery page size | 30 items |
| Failed logins before a ban | 10 from one address; the count starts over after 15 minutes without a failure |
