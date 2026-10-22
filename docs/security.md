# Security model

How MediaIndex protects accounts, media and the server, and where its
limits are. To report a vulnerability, see
[SECURITY.md](../SECURITY.md).

**Contents**

- [Who it's built for](#who-its-built-for)
- [Accounts and sessions](#accounts-and-sessions)
- [Logins and bans](#logins-and-bans)
- [Reverse proxy and client addresses](#reverse-proxy-and-client-addresses)
- [Web protections](#web-protections)
- [Private media](#private-media)
- [Site logins](#site-logins)
- [The bots](#the-bots)
- [Containers](#containers)
- [Known limitations](#known-limitations)

## Who it's built for

MediaIndex is made for private use by one to five people who know each
other, with accounts the admin creates. The security model follows from
that:

- **People without an account are kept out.** That is what the login, the
  bans, the reverse-proxy handling and the web protections below are for.
- **Accounts are kept apart where it matters:** private media, WebDAV
  folders and linked chat accounts belong to one account only.
- **The admin and the operator are trusted.** Admins can edit and delete
  anyone's items. Whoever has the database and the media files can decrypt
  private media, and the site logins the bots use are shared by everyone.
- **Accounts are trusted not to abuse the server.** There are no storage
  quotas, so any account can fill the disk, and rate limits count per
  address, not per account.

Don't open registration, or give accounts to people you don't know: the
app isn't built for that.

## Accounts and sessions

- Passwords are stored as Werkzeug hashes and must be at least 8
  characters long.
- Registration is closed (`REGISTRATION_OPEN`); admins create accounts.
  The first account ever created becomes an admin.
- An account an admin creates starts with a password the admin knows, and
  so does one whose password an admin resets on the admin panel. Its next
  login leads only to a form to choose a new one: every other page,
  `/login` included, redirects there until it is done. The account's app
  password works meanwhile, so the bots' account isn't held up.
- Users change their own password on **Settings**. It takes the current
  password, and a wrong one counts as a failed sign-in toward the [IP
  ban](#logins-and-bans).
- The session cookie is `Secure`, `HttpOnly` and `SameSite=Lax`. A login
  lasts `SESSION_DAYS` (30) days after the last visit; the server refuses
  older session cookies.
- Any password change, on **Settings**, on the admin panel or with
  `set_password.py`, logs that user out everywhere: each session carries a
  stamp that the change invalidates. Only the browser in which users
  change their own password stays signed in.
- Programs (WebDAV clients, the bots, scripts) never use the login
  password. They use a separate **app password** per account, which can be
  revoked on its own.

## Logins and bans

- **Rate limits** (per client address, stored in Redis):

  | Action | Limit |
  |---|---|
  | log in | 5 per minute |
  | change your password | 5 per minute |
  | register | 5 per hour |
  | generate an app password | 5 per hour |
  | link Telegram, request a Discord link code | 20 per hour each |
  | redeem a Discord link code (bot only) | 30 per minute |
  | API calls (upload, lookups, site logins) | 30 to 120 per minute |

- **IP bans:** 10 failed sign-ins from one address ban it for
  `BAN_DURATION_HOURS` (24). Failures count from the login form, the
  password forms, WebDAV and the API alike, and the count starts over
  after 15 minutes without a
  failure. A ban applies to every request, WebDAV included. Admins can
  lift bans and ban addresses permanently.
- If the database can't be reached to check a ban, the request is let
  through and the error is logged: the app stays available rather than
  locking everyone out.
- **Security log:** every failed sign-in, with the username tried and the
  address, every successful login, every ban, and every change to accounts
  and passwords goes to a file per day that is never deleted. See
  [Security log](operations.md#security-log).

## Reverse proxy and client addresses

Bans and rate limits work on the client's address. Behind a reverse proxy
the app sees the proxy's address, and the client's address arrives in the
`X-Forwarded-For` header, which anyone can send.

- The app believes `X-Forwarded-*` only from peers listed in
  `TRUSTED_PROXIES`, and only the last address the proxy added. From any
  other peer, the headers are removed and its real address is used.
- `TRUSTED_PROXIES` defaults to loopback and the private ranges. On a
  Docker network shared with other containers, that trusts all of them,
  so set it to your reverse proxy's fixed address.
- The compose file publishes the app on `127.0.0.1` only. Publishing it on
  all interfaces would let clients reach it without the proxy.

## Web protections

- All forms are protected against CSRF. `POST /api/upload` and the other
  API endpoints are exempt, because they authenticate with an app password
  in each request and never with a browser cookie.
- The Content Security Policy only allows scripts from the site itself; no
  inline scripts or event handlers. Tags are free text, so this backs up
  the escaping of everything shown on the page.
- Other headers: `Strict-Transport-Security` (one year, with
  `includeSubDomains` and `preload`), `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  strict-origin-when-cross-origin`, and `X-Robots-Tag: noindex` so search
  engines stay away. `X-XSS-Protection` is set to `0` on purpose: the old
  browser filter it controls caused problems of its own.
- Pages are never cached. Media and thumbnails are cached privately by the
  browser for a year: their names are content hashes, so a file never
  changes under the same name.
- Every media and thumbnail request checks that the viewer may see that
  item.

## Private media

- Each user has an AES-256 key, stored in the database. Private files and
  their thumbnails are encrypted with it in CTR mode and stored with an
  `.enc` suffix.
- CTR mode lets the server decrypt any byte range directly, so private
  videos stream and seek without a decrypted copy on disk. When a
  thumbnail has to be rendered from a private video, the decrypted copy
  lives in a temporary file that is deleted right after.
- Private items are filtered out for everyone but their owner in every
  query: gallery, feed, tag counts, the API and direct file requests.
- The keys are in the database, so anyone with a database dump and the
  media files can decrypt private media. Protect the backups accordingly;
  see [Operations](operations.md#backups).

## Site logins

The `cookies.txt` files on **Admin → Site logins** are encrypted with
AES-GCM, using a key derived from `SECRET_KEY`. A database dump alone
holds no usable session. The bots get a login only for the link they are
downloading, write it to a private temporary file for that one download,
and delete it afterwards.

## The bots

The bots feed outside media into yt-dlp and ffmpeg, so who may use them is
limited:

- The **Discord bot** only reads messages from members holding
  `DISCORD_INGEST_ROLE`, and only accepts DMs from them.
- The **Telegram bot** only handles messages from users who linked their
  account on the index. Anything else is dropped before a download.
- Both bots refuse to fetch links that point at private or internal
  addresses; see [Link downloads](link-downloads.md#which-addresses-are-fetched).
- The bots have no database access. They use the web app's API with an
  app password, and those endpoints only answer accounts listed in
  `DELEGATED_UPLOADERS`.

## Containers

| Container | Runs as | Capabilities |
|---|---|---|
| `web` | root | only `DAC_OVERRIDE`, so it can write to bind mounts owned by another user. If you give the mounts to the container's user, you can drop it too. |
| `discord-bot` | root | none |
| `telegram-bot` | user 101 | none |
| `telegram-bot-api` | user 101 after start | `CHOWN`, `SETUID` and `SETGID`, which it needs to switch to that user |
| `bgutil` | image default | none |

These all run with `no-new-privileges`. Postgres, Redis, `bgutil` and the
Telegram Bot API server are only on the internal `backend` network, and
Redis requires a password. Only the web app is published, on loopback.

## Known limitations

- **Private media is encrypted, not authenticated.** CTR mode keeps the
  content secret, but doesn't detect a file that was altered on disk.
- **The keys sit next to the data.** Encryption at rest protects against
  someone who gets the media files without the database.
- **The web app runs as root** inside its container, with one capability.
  The compensating controls are the dropped capabilities,
  `no-new-privileges`, and the bot gates above.
- **DNS rebinding.** The address checks look up a host before the request.
  A server that answers differently between the check and the request can
  get around them.
- **Tags are one text field per item.** Searching scans that field, which
  is fast for a personal library but would need a different design for
  hundreds of thousands of items.
- **Background work runs inside the web app**: video thumbnails and the
  WebDAV scanner are threads in the gunicorn workers. A thumbnail job lost
  to a restart leaves the placeholder, which [Integrity](admin.md#integrity)
  finds and renders again.
