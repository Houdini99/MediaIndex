# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-10-21

### Initial Stable Release Summary

MediaIndex is a self-hosted, login-only gallery for images, GIFs and
videos, made for private use by one to five people who want their media
tagged, searchable and under their own control. It runs as a Docker
Compose stack behind a reverse proxy, and takes media in from the browser,
a network folder, Discord and Telegram.

#### Core capabilities

- [Gallery](docs/using.md#gallery) with infinite scrolling, tag search,
  tag exclusion, type filters, newest, oldest and random order, a *Hide AI*
  filter, and bulk delete, tagging and privacy changes.
- [Vertical feed](docs/using.md#feed) with autoplay, a custom video player,
  include and exclude filters, slideshow, fullscreen and keyboard
  shortcuts.
- [Tag list](docs/using.md#tags) with usage counts.
- [Upload page](docs/using.md#upload) for many files at once, with tags and
  a private option per file; duplicates are recognised by content hash.
- [Private media](docs/using.md#private-media), encrypted on disk with a
  key per user (AES-256-CTR), streamed and seekable without a decrypted
  copy, and reversible after upload.
- [WebDAV inbox](docs/webdav.md): a personal network folder per user, with
  an *Untagged* queue to tag files before they are indexed.
- [Duplicate finder](docs/admin.md#duplicate-finder) using perceptual
  hashes, which recognises re-encoded videos and frames taken from them,
  with a quarantine that can be undone.

#### Supported integrations

- [Discord bot](docs/discord-bot.md): announcements of new public media,
  ingest from a channel for members with a chosen role, private ingest by
  direct message, verified account linking, tags from message text, and
  retries for temporary failures.
- [Telegram bot](docs/telegram-bot.md): ingest by private chat for linked
  users, tags from captions, albums, and files up to 2000 MB through a
  local Bot API server.
- [Link downloads](docs/link-downloads.md) in both bots: direct file links,
  video sites through yt-dlp, links that serve a file without naming one,
  stored site logins, YouTube PO tokens, and a resolution cap that prefers
  H.264.
- [Upload API](docs/api.md#upload-api) for scripts, authenticated with an
  app password.

#### Administration

- [Admin panel](docs/admin.md): users and admins, password resets, user
  deletion with a choice to keep their media, IP bans, tag rename and
  removal, an integrity check with one-click fixes, site logins, and a log
  viewer.
- [Maintenance scripts](docs/admin.md#maintenance-scripts) for passwords,
  tags and thumbnails.

#### Security

- [Sessions and logins](docs/security.md#accounts-and-sessions): closed
  registration, a new password chosen at the next login after an admin
  creates an account or resets its password, a password change on
  Settings, rolling sessions that a password change ends everywhere, and
  separate app passwords for programs.
- [Rate limits and IP bans](docs/security.md#logins-and-bans) on logins,
  WebDAV and the API.
- [Security log](docs/operations.md#security-log): every failed and
  successful sign-in with the username and address, bans and account
  changes, one file per day that is never deleted.
- [Trusted-proxy handling](docs/security.md#reverse-proxy-and-client-addresses),
  so clients can't choose the address that bans and limits apply to.
- [Web protections](docs/security.md#web-protections): CSRF tokens, a
  Content Security Policy without inline scripts, and strict security
  headers.
- [Site logins](docs/security.md#site-logins) encrypted with AES-GCM under
  a key derived from `SECRET_KEY`.
- [Hardened containers](docs/security.md#containers) with dropped
  capabilities and `no-new-privileges`, and Redis protected by a password
  on an internal network.

#### Deployment

- [Docker Compose and Portainer](docs/installation.md) stack: the web app,
  PostgreSQL 17, Redis, daily `pg_dump` backups, and the optional bots,
  Telegram Bot API server and PO-token service.
- [Configuration](docs/configuration.md) through environment variables,
  and [operations](docs/operations.md) guidance for backups, restores,
  PostgreSQL upgrades and logs.

#### Known limitations

- Built for private use by one to five people who trust its admin: there
  are no storage quotas and no email.
- Private media is encrypted but not authenticated, and the keys are
  stored in the same database as the media records.
- The web app runs as root inside its container, with one capability.
- The bots' address checks don't stop DNS rebinding, and the Discord bot
  doesn't apply them to links that end in a media file extension.
- Tags are stored as one text field per item, which suits a personal
  library rather than hundreds of thousands of items.

See [Security model](docs/security.md#known-limitations) for details.

[1.0.0]: https://github.com/Houdini99/MediaIndex/releases/tag/v1.0.0
