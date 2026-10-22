# Development

How to run MediaIndex locally, run the tests, and find your way around the
code. For contributing, see also [CONTRIBUTING.md](../CONTRIBUTING.md).

**Contents**

- [Run the web app locally](#run-the-web-app-locally)
- [Tests and lint](#tests-and-lint)
- [Project layout](#project-layout)
- [Design rules](#design-rules)
- [Working on the bots](#working-on-the-bots)

## Run the web app locally

You need Python 3.11, ffmpeg (for video thumbnails), Docker (for a
throwaway PostgreSQL), and optionally a Redis server.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

docker run --rm -d --name mediaindex-dev-pg -p 127.0.0.1:5432:5432 \
  -e POSTGRES_PASSWORD=dev postgres:17-alpine

export DATABASE_URL=postgresql://postgres:dev@127.0.0.1:5432/postgres
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export DEV_MODE=true     # session cookies over plain HTTP
.venv/bin/python run.py  # prints the address it serves on, port 5001
```

The schema is created on first start. Without Redis at `REDIS_URL`
(default `redis://localhost:6379`), rate limits and WebDAV locks don't
work; start one with `docker run --rm -d -p 127.0.0.1:6379:6379 redis:7-alpine`.

To create the first account, start the app with `REGISTRATION_OPEN=true`
and register; the first account becomes an admin.

## Tests and lint

The tests need a PostgreSQL server; each run creates a throwaway database
on it and drops it afterwards. `make test` starts a server for you, as the
container `mediaindex-test-pg` on `127.0.0.1:5433`, and reuses it on later
runs.

```bash
make test            # the whole suite
make test ARGS="tests/test_sniff.py -q"   # pass arguments to pytest
make test-fast       # skip the tests that wait for a decoder deadline
make db-down         # remove the test database container
make lint            # ruff (unused and undefined names) and a JS syntax check
```

The Makefile uses `.venv/bin/python`; override it with
`make test PY=python3`. Without `make`, start a PostgreSQL server yourself
and run:

```bash
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \
  .venv/bin/python -m pytest
```

The bots' libraries (discord.py, python-telegram-bot, aiohttp, yt-dlp)
don't need to be installed: `tests/conftest.py` stubs whichever are
missing, so the suite runs offline. Tests that need the real yt-dlp or
ffmpeg are skipped when they aren't installed.

## Project layout

```
run.py                 entry point for local development (gunicorn in Docker)
app/
  __init__.py          create_app(): extensions, blueprints, WebDAV mount,
                       proxy handling, security headers
  config.py            settings from the environment
  db_utils.py          connection pool and the schema
  proxy.py             X-Forwarded-* only from trusted peers
  notifications.py     queues announcements for the Discord bot (Redis)
  site_logins.py       the bots' site logins: filtering, encryption, revisions
  logging_setup.py     log/app.log and its rotation
  security_log.py      log/security/: sign-ins, bans, account changes
  auth/                login, registration, bans; db.py holds the user SQL
  media/               gallery, feed, tags, upload, files, APIs
    db.py              media SQL            ingest.py   store → thumbnail → encrypt → index
    processing.py      hashing, thumbnails  crypto.py   AES-CTR, range decryption
    ffmpeg.py          ffmpeg with deadlines  phash.py  perceptual hashes
    tagging.py         the tag-string rules  tasks.py   video-thumbnail worker
    staging*.py        WebDAV inbox scanner, SQL and routes
    dupes_db.py        duplicate-finder SQL  thumb_repair.py  thumbnail repairs
  admin/               admin panel, integrity check, duplicate finder, site logins
  dav/                 WebDAV: app-password auth and per-user folders
  main/                favicon, robots.txt
  templates/, static/  Jinja2 templates, CSS and plain JavaScript (no build step)
bots_common/           shared by both bots: yt-dlp resolver, link and tag
                       parsing, sniff.py (link probing and address checks),
                       download.py (checked, capped downloads), env.py
                       (reading settings), site_logins.py, ytdlp_fixes.py
                       (extractor stopgaps)
discord_bot/           the Discord bot (its own image)
telegram_bot/          the Telegram bot (its own image)
scripts/               maintenance scripts (run inside the web container)
tests/                 pytest suite
```

Runtime folders (`media_files/`, `thumbnails/`, `staging/`, `log/`,
`data/`, `backups/`) are created by the app or the stack and are ignored
by git.

## Design rules

- **Routes contain no SQL.** All database access lives in `auth/db.py`,
  `media/db.py`, `media/staging_db.py` and `media/dupes_db.py`; file and
  image processing in `media/processing.py` and `media/phash.py`.
- **Tags have one set of rules,** in `app/media/tagging.py`: a tag is the
  trimmed text between commas, and stored tag strings are always in that
  form. Filters compare whole tags by putting commas around the stored
  string, which only works because of that.
- **Media rows are mappings,** not tuples, so nothing depends on column
  order.
- **ffmpeg is never trusted to finish.** Every decode goes through
  `media/ffmpeg.py`, which kills it at a deadline.
- **Private items are filtered in SQL,** in every query, not in the
  templates.
- **No inline JavaScript.** The Content Security Policy forbids it, and
  `tests/test_no_inline_js.py` fails if an inline handler appears. Use
  `data-action` and the dispatchers in `static/js/common.js`.
- **Schema changes are idempotent DDL** in `db_utils.py`, run at startup
  under an advisory lock. There is no separate migration step.

## Working on the bots

- Both bot images build from the repository root, because they include
  `bots_common/`. Use `./build.sh discord` or `./build.sh telegram`; by
  hand that is `docker build -f discord_bot/Dockerfile -t index-discord-bot .`.
- Code both bots need belongs in `bots_common/`. `tests/test_bots_common.py`
  fails if a bot grows its own copy of the resolver, the parsers or the
  settings readers.
- The bots only talk to the index through its HTTP API
  ([API](api.md#bot-endpoints)), never to the database.
