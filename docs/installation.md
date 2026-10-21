# Installation

MediaIndex runs as a Docker Compose stack behind a reverse proxy that
terminates HTTPS. This page sets it up with `docker compose`, or as a
Portainer stack, and creates the first admin account.

It is meant for private use by one to five people who know each other;
see [Who it's for](../README.md#who-its-for) before you set it up for
anyone else.

**Contents**

- [What you need](#what-you-need)
- [The stack](#the-stack)
- [Set up with Docker Compose](#set-up-with-docker-compose)
- [Set up with Portainer](#set-up-with-portainer)
- [Put it behind the reverse proxy](#put-it-behind-the-reverse-proxy)
- [Create the first admin](#create-the-first-admin)
- [Update](#update)

## What you need

- A Linux host with Docker and the Compose plugin.
- A reverse proxy with HTTPS in front of it, such as Nginx Proxy Manager,
  Caddy or Traefik, on a Docker network the stack can join.
- Disk space for your library. Media, thumbnails and the WebDAV inbox live
  in folders next to the checkout; the database lives in a Docker volume.

Video thumbnails and link downloads use ffmpeg, which is inside the images.
Nothing else needs to be installed on the host.

## The stack

[`docker-compose.yml.example`](../docker-compose.yml.example) defines these
services:

| Service | Image | Needed for |
|---|---|---|
| `web` | `index-app`, built here | the app itself |
| `postgres` | `postgres:17-alpine` | the database |
| `postgres-backup` | `prodrigestivill/postgres-backup-local` | daily database dumps |
| `redis` | `redis:7-alpine` | rate limits, WebDAV locks, the Discord announcement queue |
| `discord-bot` | `index-discord-bot`, built here | the [Discord bot](discord-bot.md) |
| `telegram-bot` | `index-telegram-bot`, built here | the [Telegram bot](telegram-bot.md) |
| `telegram-bot-api` | `aiogram/telegram-bot-api` | Telegram files up to 2000 MB |
| `bgutil` | `brainicism/bgutil-ytdlp-pot-provider` | YouTube [link downloads](link-downloads.md#youtube-po-tokens) without a login |

The compose file uses images rather than building them, so it also works
as a Portainer web-editor stack. That is why you build the three local
images with `./build.sh` first.

Two networks: `proxy` is shared with your reverse proxy and must already
exist; `backend` is internal and is the only network Postgres, Redis,
`bgutil` and the Telegram Bot API server are on.

The web app, the bots, `bgutil` and the Telegram Bot API server run with
`no-new-privileges` and drop every Linux capability they don't need. See
[Security](security.md#containers) for the details.

**You don't need the bots?** Delete `discord-bot`, `telegram-bot`,
`telegram-bot-api` and `bgutil` from your copy of the compose file. Keep
the rest. With only one bot, delete the other bot's services, and delete
`bgutil` only if neither bot is left (then also remove it from the remaining
bot's `depends_on` and set its `*_YTDLP_POT_PROVIDER` to an empty value).

## Set up with Docker Compose

1. **Get the code.**

   ```bash
   git clone https://github.com/Houdini99/MediaIndex.git
   cd MediaIndex
   ```

2. **Create your compose and environment files.** Both copies are
   ignored by git, so your settings never end up in a commit.

   ```bash
   cp docker-compose.yml.example docker-compose.yml
   cp .env.example .env
   ```

   Remove the services you don't need (see above), then fill in the
   required variables in `.env`. [Configuration](configuration.md) lists
   them all. At the least:

   - `SECRET_KEY`, `REDIS_PASSWORD`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
     and `DATABASE_URL`;
   - if you keep the Discord bot: `DISCORD_BOT_TOKEN`,
     `DISCORD_NOTIFY_CHANNEL_ID`, `DISCORD_INGEST_CHANNEL_ID` and
     `DISCORD_INGEST_ROLE` (see [Discord bot](discord-bot.md#setup)). The
     compose file refuses to start *any* service while these are missing,
     so set them now or remove the `discord-bot` service;
   - if you keep the Telegram bot: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`
     and `TELEGRAM_API_HASH` (see [Telegram bot](telegram-bot.md#setup)).

   Leave `INDEX_BOT_APP_PASSWORD` empty for now: the bots' app password
   can only be generated once the web app runs (step 6).

3. **Build the images.** `./build.sh` builds the web app and both bots
   from the repository root. `./build.sh web` builds only the web app.

   ```bash
   ./build.sh
   ```

4. **Create the shared network** once, unless your reverse proxy already
   has one called `proxy`. If it uses another name, change `proxy` in the
   compose file instead.

   ```bash
   docker network create proxy
   ```

5. **Start the stack.**

   ```bash
   docker compose up -d web postgres postgres-backup redis
   ```

   The web app creates its database schema on first start. The app listens
   on `127.0.0.1:5001` on the host, so `curl http://127.0.0.1:5001/login`
   should answer `200`. Then [put it behind the reverse
   proxy](#put-it-behind-the-reverse-proxy) and [create the first
   admin](#create-the-first-admin).

6. **Add the bots.** Log in as an admin, create an account for the bots on
   **Admin → Users** (the default name is `Bot`), log in as that account,
   give it a new password when asked (the bots never use it), and generate
   its app password on **Untagged** (`/queue`). Put it in
   `INDEX_BOT_APP_PASSWORD`, then follow the setup steps on the
   [Discord bot](discord-bot.md#setup) and [Telegram
   bot](telegram-bot.md#setup) pages and start everything:

   ```bash
   docker compose up -d
   ```

Changes for your machine only can also go in a
`docker-compose.override.yml`, which `docker compose` merges in
automatically and git ignores.

## Set up with Portainer

Portainer's web-editor stacks deploy images and don't build them, which is
the shape the compose file already has.

1. On the Docker host, clone the repository and build the images, as in
   steps 1 and 3 above.
2. **Stacks → Add stack → Web editor.** Paste
   [`docker-compose.yml.example`](../docker-compose.yml.example) and remove
   the services you don't need.
3. Under **Environment variables**, set `INDEX_ROOT` to the absolute path
   of the checkout on the host. Portainer resolves relative paths against
   its own data directory, not your checkout. Then set the required
   variables from [Configuration](configuration.md). There is no `.env`
   file in this flow.
4. Deploy the stack, then continue with the reverse proxy and the first
   admin below.

Don't rename the stack later: Portainer prefixes the `pg_data` volume with
the stack name, so a rename leaves the database behind in the old volume.

A *Repository* stack can build the images itself instead. In that case add
a `build:` section to `web` (`context: .`) and to each bot (`context: .`
with `dockerfile: discord_bot/Dockerfile` or
`dockerfile: telegram_bot/Dockerfile`). Every image builds from the
repository root.

## Put it behind the reverse proxy

The reverse proxy reaches the app by container name over the `proxy`
network: forward your domain to `http://mediaindex-web:5001`.

- **Allow large request bodies.** Uploads can be up to 10 GiB, and WebDAV
  uploads are as large as the files people copy in. In Nginx that is
  `client_max_body_size` (for example `client_max_body_size 0;` to turn
  the limit off).
- **Use HTTPS.** The session cookie is marked `Secure`, and Windows only
  sends WebDAV passwords over HTTPS.
- **Set `TRUSTED_PROXIES`** to the proxy's address on the `proxy` network.
  The app believes `X-Forwarded-For` only from the peers listed there, and
  the default trusts every private address. See
  [Security](security.md#reverse-proxy-and-client-addresses).
- **Keep the published port on loopback.** The compose file publishes the
  app on `127.0.0.1` only, for checks from the host. Publishing it on
  `0.0.0.0` would let clients bypass the proxy. If the proxy is the only
  way in, you can remove the `ports:` mapping.

## Create the first admin

Registration is closed: admins create accounts on **Admin → Users**. The
first account is created through the registration page, opened for that
one sign-up. Whoever registers first becomes an admin automatically.

1. Set `REGISTRATION_OPEN=true` in `.env` (under Portainer, in the stack's
   environment variables) and restart the web app:

   ```bash
   docker compose up -d web
   ```

2. Open `https://<your-domain>/register` and create your account. Passwords
   need at least 8 characters.
3. Remove `REGISTRATION_OPEN` again, or set it to `false`, and restart the
   web app as in step 1, so nobody else can register.

From then on, create an account for each person you share the index with,
and make other admins, on **Admin → Users**. Each person replaces the
password you gave them with their own at their first login, and again
after you reset it there. To reset a password from the command line, see
[Admin](admin.md#maintenance-scripts).

## Update

```bash
git pull
./build.sh
docker compose up -d
```

Under Portainer, run `git pull` and `./build.sh` on the host, then use the
stack's **Update** button. Database schema changes are applied
automatically when the web app starts. Check the
[changelog](../CHANGELOG.md) for new variables before updating.

PostgreSQL is pinned to major version 17. A newer major version can't read
the existing data directory; see
[Operations](operations.md#postgresql-major-versions) before changing it.
