# Operations

Running the stack day to day: backups, restores, PostgreSQL upgrades, logs,
and checking a deployment. To install or update, see
[Installation](installation.md).

**Contents**

- [What holds your data](#what-holds-your-data)
- [Backups](#backups)
- [Restore the database](#restore-the-database)
- [PostgreSQL major versions](#postgresql-major-versions)
- [Logs](#logs)
- [Security log](#security-log)
- [Check a deployment](#check-a-deployment)

## What holds your data

| Where | What |
|---|---|
| `pg_data` volume | the database: users, media records, tags, keys for private media, site logins |
| `media_files/` | the media files, named after their SHA-256 hash; private ones end in `.enc` |
| `thumbnails/` | thumbnails, and `thumbnails/staging/` for WebDAV inbox previews |
| `staging/` | the WebDAV inboxes, one folder per user |
| `backups/postgres/` | the daily database dumps |
| `log/` | the application log, and in `log/security/` the [security log](#security-log), which is never deleted |
| `data/` | a lock file the web app's workers share |
| `redis_data`, `telegram_api_data` volumes | queues and temporary files; nothing to back up |

The folders are bind mounts under `INDEX_ROOT`, by default the checkout.

## Backups

The `postgres-backup` service dumps the database every day into
`backups/postgres/`, keeping 14 daily, 8 weekly and 6 monthly dumps.

- **The dumps contain every user's encryption key and password hash.**
  Treat the folder as a secret: keep it readable by its owner only
  (`chmod 700`) and out of unencrypted syncs.
- **The dumps don't contain the media.** Back up `media_files/` and
  `thumbnails/` separately, and `staging/` if you want the WebDAV inboxes
  too. A database dump alone restores the index, not the files it points
  to.
- **Back up `SECRET_KEY` as well.** Without the same key, the stored site
  logins can't be decrypted. Everything else works with a new key, but all
  users are logged out.

## Restore the database

A dump contains no `DROP` statements, so restore it into an empty
database. With the stack's own names, and `POSTGRES_USER` set in your
shell:

```bash
docker compose stop web discord-bot telegram-bot
docker exec mediaindex-postgres dropdb -U "$POSTGRES_USER" mediaindex
docker exec mediaindex-postgres createdb -U "$POSTGRES_USER" mediaindex
gunzip -c backups/postgres/daily/<file>.sql.gz \
  | docker exec -i mediaindex-postgres psql -U "$POSTGRES_USER" -d mediaindex
docker compose start web discord-bot telegram-bot
```

Restore the media folders from the same point in time, so that records and
files match. Afterwards, [Integrity](admin.md#integrity) shows any
mismatch.

## PostgreSQL major versions

The compose file pins `postgres:17-alpine`. Pulling a newer 17.x is safe.
A new **major** version (18 and later) can't read the data directory of
17, so the database would not start. To move to a new major version:

1. Take a fresh dump (see above), and stop the stack.
2. Change the image of `postgres` to the new version, and the image of
   `postgres-backup` to the matching tag.
3. Start `postgres` with an empty data volume (rename or remove `pg_data`).
4. Restore the dump as above, then start the rest of the stack.

Never change the major version as part of a routine update.

## Logs

The web app writes `log/app.log`. It is an **error log**, not an access
log: warnings, errors and anything the app prints. A quiet `app.log` means
no errors, not no traffic. Requests are logged by gunicorn to the
container's output:

```bash
docker logs mediaindex-web
```

`app.log` is rotated at midnight into `app.log.YYYY-MM-DD`, and rotated
files are deleted after 14 days. Admins can read all of them on **Admin →
Logs**.

The bots and the other services log to their container output only
(`docker logs mediaindex-discord-bot`, and so on).

## Security log

Sign-ins, failed sign-ins, bans and account changes go to their own files,
one per day (UTC): `log/security/security-YYYY-MM-DD.log`. **The app never
rotates or deletes them**, so after an attack you can still see weeks later
what was tried, from where, and whether anything got in. Admins can read
them on **Admin → Logs** too.

Each line is one JSON object:

```json
{"time": "2026-10-21T13:19:55Z", "event": "login_failed", "ip": "203.0.113.7", "user": "admin", "known": false, "via": "login", "ua": "python-requests/2.32.3"}
```

| Field | Meaning |
|---|---|
| `time` | When, in UTC |
| `event` | What happened; see the next table |
| `ip` | The client's address, as the app sees it behind the [trusted proxy](security.md#reverse-proxy-and-client-addresses) |
| `user` | The account concerned. For a failed sign-in, whatever was typed as the username, whether or not that account exists |
| `known` | Whether that username exists. Many `false` means someone is guessing names; `true` means they know a real one |
| `via` | Where a sign-in was tried: `login` (the login form), `password-change` (a wrong current password on a password form), `webdav` or `api` (a wrong app password) |
| `by` | The admin who made a change, or `command line` for the maintenance scripts |
| `ua` | The client's User-Agent, which often names the tool an attacker uses |

Long values are cut at 100 characters and end in `…`. Fields that don't
apply to an event are left out.

| Event | When |
|---|---|
| `login_failed` | A wrong password or app password, through any of the `via` routes above |
| `login` | A successful sign-in on the login form |
| `ip_banned` | An address was banned (`address`): automatically after 10 failures (`failures`, `hours`), or by an admin (`manual`) |
| `ip_unbanned` | An admin lifted a ban |
| `password_changed` | A user changed their own password (`via`: `first-login` or `settings`) |
| `password_reset` | An admin or a maintenance script set a user's password |
| `user_created`, `user_deleted` | An account was added (`via`: `admin` or `register`) or removed (`media`: what happened to its media) |
| `admin_granted`, `admin_revoked` | Admin rights changed |
| `app_password_created`, `app_password_revoked` | A user generated or revoked their app password |

Successful app-password sign-ins (WebDAV clients, the bots) aren't logged:
they happen on every request.

The files belong to the container's user and are readable by it only, so
on the host read them with `sudo`. With `jq` installed:

```bash
# Every failed sign-in today
sudo cat log/security/security-$(date -u +%F).log | jq -c 'select(.event == "login_failed")'
# The usernames tried, most often first
sudo cat log/security/*.log | jq -r 'select(.event == "login_failed") | .user' | sort | uniq -c | sort -rn
# The addresses they came from
sudo cat log/security/*.log | jq -r 'select(.event == "login_failed") | .ip' | sort | uniq -c | sort -rn
# Every successful sign-in, to check none came from those addresses
sudo cat log/security/*.log | jq -c 'select(.event == "login")'
```

Without `jq`, `sudo grep -h '"login_failed"' log/security/*.log` works too.

Keep in mind:

- **A password typed into the username box ends up here.** That's why the
  files are readable by their owner only. Treat them like the [database
  dumps](#backups), and back them up the same way if you want to keep them.
- **They keep growing.** A normal day is a handful of lines of about 200
  bytes each. The rate limits and bans cap what one address can try at a
  few hundred failures a day, under 200 KB of log; the files only grow
  fast when many addresses attack at once. To free space, archive or
  delete old days yourself; the app never will.

## Check a deployment

```bash
# The web app keeps one capability (DAC_OVERRIDE), the bot none.
docker exec mediaindex-web grep CapEff /proc/1/status            # 0000000000000002
docker exec mediaindex-discord-bot grep CapEff /proc/1/status    # 0000000000000000

# Redis requires its password.
docker exec mediaindex-redis redis-cli PING                      # NOAUTH …

# The app is published on loopback only.
docker port mediaindex-web                                       # 127.0.0.1:5001
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5001/login   # 200
```

- `PONG` instead of `NOAUTH` means Redis accepts commands without a
  password.
- `0.0.0.0` from `docker port` means the app can be reached without going
  through the reverse proxy, and with it the client's own
  `X-Forwarded-For` header. See
  [Security](security.md#reverse-proxy-and-client-addresses).
- The Discord bot's status line should say `ingest: @<role> only`; see
  [Discord bot](discord-bot.md#checking-it-works).
