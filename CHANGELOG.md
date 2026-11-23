# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.0] — 2026-11-23

The gallery's toolbar stays at the bottom of the screen, so search,
filters and Select are in reach however far you scroll. Videos in the
gallery's full-size view play on a loop. `.m4v` videos are accepted
everywhere and play like MP4.

### Upgrading from 1.3.0

- Rebuild the web app and both bot images with `./build.sh`. The database
  is untouched.

### Added

- `.m4v` videos. Uploads, the WebDAV inbox and both bots accept them, and
  they play inline like MP4. The web app serves them as `video/mp4`: its
  image has no system MIME table, and Python's own has no `.m4v`, so
  they would otherwise go out as `application/octet-stream`. A link that
  names no file and answers `video/x-m4v` is ingested as `.m4v`.

### Changed

- The gallery's toolbar (search, filters, Select) is docked to the bottom
  of the screen instead of scrolling away with the top of the page. In
  select mode the selection bar sits on top of it. On a phone it sits
  above the tab bar, the folded filters open upward, and a tap outside
  folds them again.
- Videos in the gallery's full-size view play on a loop, as they already
  did in the feed. A slideshow still plays each video once before it moves
  on, and stopping the slideshow turns the loop back on.

## [1.3.0] — 2026-11-21

The web app works properly on a phone now. Page links sit in a tab bar
at the bottom of the screen instead of a top bar that hid most of them.
The gallery's filters fold away, and the lightbox fills the screen and
responds to swipes and the Back gesture. The feed runs from the top of
the screen down to the tab bar. Uploads show their progress, and the admin
tables turn into cards you can read without scrolling sideways. Desktop
layouts are unchanged apart from the fixes below.

### Upgrading from 1.2.0

- Rebuild the web app image with `./build.sh`. The bots and the database
  are untouched.

### Added

- Phone layout (screens up to 760px wide). A slim top bar with the
  account button, and Gallery, Feed, Upload, Untagged and Tags as a tab
  bar along the bottom edge. Between 761px and
  1023px, Settings, Admin Panel and Sign Out move into a menu behind the
  user chip, so the top bar no longer runs off the screen.
- "Add to Home Screen" opens MediaIndex full screen like an installed
  app, with its own icon (a web app manifest and touch icons).
- Gallery on a phone: the search box, Filters and Select share one row,
  and the other filters open under Filters, which shows how many are
  set. Cards are two to a row with their tags kept to two lines.
- Lightbox: on a phone it fills the screen with icon buttons. Swipe
  sideways for the next or previous file and swipe down to close. The
  Back button or gesture closes it at any width, instead of leaving the
  gallery and every page infinite scroll had loaded.
- Lightbox: "Edit tags" edits the open file's tags in place. Edit and
  Delete only show on files you own (or as an admin).
- Upload: a progress bar with bytes sent, then "Processing…" while the
  server works. The results show on the same page. On a phone the drop
  zone says "tap to pick" rather than "drag", previews stay compact rows,
  and the upload button stays in reach above the tab bar.
- Feed on a phone: slides run from the top edge to the tab bar, and the
  settings open as a sheet from the bottom. Fullscreen also hides the
  browser's bars where the browser allows it, and its button stays on
  screen as the way out, since a phone has no Escape key. The video
  scrubber is easier to grab with a finger.
- Admin pages on a phone: every table row becomes a card with labelled
  values and full-width actions, the tabs scroll sideways in one row,
  and the forms use the full width.

### Changed

- Deleting a file or saving its tags in the gallery updates the page in
  place instead of reloading it, so you keep your place.
- Every form field is 16px on a phone, so iOS no longer zooms the page
  in when you tap into one.
- Hover effects (cards lifting, buttons growing) only happen with a
  mouse. On a touch screen they used to stick to whatever was tapped
  last.
- `POST /upload` and `POST /edit/<id>` answer scripted callers
  (`X-Requested-With: XMLHttpRequest`) with JSON. Upload returns the
  result messages and per-status counts, edit returns the stored tags.
  `/mediadata/<id>` gains `can_edit`. Plain form posts behave as before.

### Fixed

- Buttons with the `hidden` attribute still showed, for example "Cancel
  scan" and the groups pager on Near Duplicates, or Revoke with no
  WebDAV password set.
- The feed's slideshow and fullscreen buttons drew both of their icons on
  top of each other.
- In the feed, a private file's badge sat under the toolbar, and the
  "Scroll" hint covered the tags.
- The gallery toolbar's type and sort menus took a full row each.
- The admin Users table ran wider than the screen at tablet widths.

## [1.2.0] — 2026-11-20

Both bots can now download from Bunkr, single files and whole albums.
Before, a Bunkr link was indexed as a broken video: the bots saved the
site's web page under the video's name. A link that answers with a web
page instead of a file is now refused on every site. In Discord, an
album reports into one status message instead of a line per file, and in
Telegram, a reply to the tag prompt tags everything the prompt asked
about.

### Upgrading from 1.1.0

- Rebuild the bot images with `./build.sh`. Only the bots changed: the
  web app image and the database are untouched.
- Optional: add `DISCORD_ALBUM_MAX_FILES` and `TELEGRAM_ALBUM_MAX_FILES`
  to your compose file (both default to 20).

### Added

- Both bots download Bunkr files. A Bunkr link is a page, even when it
  ends in `.mp4`, so the bots used to index that page as a broken video.
  They now ask Bunkr for the file the way its Download button does, with a
  fresh download token right before each download. File pages (`/f/…`,
  `/v/…`, `/i/…`) and Download links (`dl.bunkr…/file/<id>`) work on any
  Bunkr domain. See [Link downloads](docs/link-downloads.md#bunkr).
- Bunkr albums: an album link (`/a/…`) downloads the album's images and
  videos, each as its own item, under the message's tags. New settings
  `DISCORD_ALBUM_MAX_FILES` and `TELEGRAM_ALBUM_MAX_FILES` (default 20)
  cap the files taken from one album; `0` turns albums off. When files are
  left out, the bot says how many. See
  [Link downloads](docs/link-downloads.md#albums).
- Discord: an album's files report into one status message, edited as
  each file is done, instead of posting a line per file. Who the files
  were indexed for and their tags are said once at the top. A new message
  starts only when the list would pass Discord's 2000-character limit.

### Fixed

- A direct file link that answers with a web page instead of the file is
  refused with a 🚫 or a reply, instead of being indexed as a broken file.
- Telegram: replying to the bot's "send tags" prompt now tags everything
  that prompt asked about (a photo album, several links in one message).
  It used to tag only one of them and leave the rest waiting.

## [1.1.0] — 2026-10-22

A maintenance release from a full code audit. The Discord bot now checks
every link it downloads, both bots behave and word their messages the
same way, and a number of small bugs are fixed. Most of the work is
behind the scenes: the two bots share one download path, settings reader,
index client and yt-dlp wrapper, and the app lost its duplicated helpers,
stylesheets and dead code. The test suite grew from 954 to 1120 tests
along the way.

### Upgrading from 1.0.0

- Rebuild all three images with `./build.sh`. The bots now build from one
  Dockerfile, `bots_common/Dockerfile`; a stack that builds the bot images
  itself instead needs the change described under *Changed*.
- No database migration is needed. A database from before 1.0.0 has to
  run 1.0.0 once first (see *Removed*).
- Optional: drop `FLASK_APP` from your compose file, and set
  `DISCORD_LINK_MAX_MB` if 512 MB is not the limit you want.
- `DISCORD_LOG_CHANNEL_ID`, when set, has to be a number now.
- The tag scripts are now `scripts/tags.py rename|remove|normalize`.

### Added

- `DISCORD_LINK_MAX_MB` (default 512): the size limit for a file the
  Discord bot downloads from a link or an embed, like the Telegram bot's
  `TELEGRAM_LINK_MAX_MB`. Attachments stay limited by Discord's own upload
  limit.

### Changed

- The three tag scripts are one, `scripts/tags.py`, with the subcommands
  `rename <old> <new>`, `remove <tag>` and `normalize`, each with
  `--dry-run`. `rename_tag.py`, `remove_tag.py` and `normalize_tags.py`
  are gone. Every script that uses the database now stops with the
  `docker exec` command to run it in the web container when
  `DATABASE_URL` is not set, as the password scripts already did.
- A privacy flag now reads `yes` and any capitalisation (`TRUE`, `On`)
  as on, like the duplicate finder's switches already did: the upload
  form's checkboxes, the upload API's `private` and `private_<n>` fields
  and the queue's promote form. Before, only `1`, `on` and `true` counted.
- Both bot images build from one Dockerfile, `bots_common/Dockerfile`,
  with a `BOT` build argument (`discord` or `telegram`); `./build.sh`
  passes it. A stack that builds the bot images itself needs
  `dockerfile: bots_common/Dockerfile` and `args: {BOT: …}` instead of the
  per-bot Dockerfiles, which are gone. The Discord bot now keeps yt-dlp's
  and deno's caches under `/tmp/.cache`, like the Telegram bot.
- Messages on the sign-in pages (a wrong password, "please log in", the
  welcome on the choose-a-password page) look like the rest of the app's,
  with an icon.
- The first page of the gallery badges each item with its stored type,
  like the cards that load while scrolling. An old item whose type is
  unknown is now badged *unknown* instead of *image*.
- `/delete-bulk`, `/privacy-bulk` and `/edit-bulk` answer a body without a
  usable `ids` list with one message, "ids must be a non-empty list of
  integers", instead of two.
- The gallery, feed and lightbox APIs read each item's stored type instead
  of working it out from the file name for every row, and the duplicate
  finder counts items per type in one query.
- After the index rejects a bot's app password, the bot pauses for
  `AUTH_RETRY_SECONDS` counted from that rejection, and its "backing off"
  lines name the seconds actually left. Before, the bots kept a second
  clock that each message arriving during the pause could start over
  (a DM, or a Telegram sender whose account link was not cached), so a
  busy bot stayed paused for longer.
- The bots word a failed link download the same way. A link that hangs
  before downloading reads "it hung for 1800s without downloading
  anything" on Discord too, and an unexpected failure reads "unexpected
  error: … (see container log)" on Telegram too.
- When the index answers an upload with 404 (in practice a wrong
  `INDEX_INTERNAL_URL`), the Discord bot fails that file with "the index
  returned HTTP 404", like the Telegram bot and any other refused upload.
  Before, it stopped calling the index for `AUTH_RETRY_SECONDS`, as if its
  app password had been rejected, and asked for an index upgrade.
- A `DISCORD_LOG_CHANNEL_ID` that is not a number now stops the Discord
  bot at startup with a message naming the variable, like the bots' other
  number settings. Before, it turned the log channel off without a word.
- Code clean-up from the audit, with no change in behaviour:
  - Bots: one download path (one downloader class, and one redirect
    walker that checks every hop), one module that reads their settings
    (`bots_common/env.py`), one client for the index's API with the
    account-link cache (`bots_common/index_client.py`), one wrapper
    around the yt-dlp download (`resolver.resolve_async()`), whose
    download step runs its first try and its impersonated retry in one
    loop, and shared tag and file-extension helpers. The upload event
    carries the item's stored type, which the Discord announcement reads
    instead of guessing it from the file name.
  - App: one helper each for decrypting a private file, parsing id
    lists, checking the bots' API accounts, checking an app password
    (WebDAV and the upload API) and deleting files (`app/files.py`); one
    place for the minimum password length (the forms, their inputs and
    `set_password.py`); one reader for the gallery's filters, shared by
    the page and its API, and one for the duplicate finder's settings;
    one deadline for the two video decodes (`ffmpeg.Deadline`) and one
    `user:<name>` helper for the stored item and its announcement.
    Logging takes its folder from `LOG_FOLDER`, and logging out leaves
    deleting the session cookie to Flask.
  - Database: the modules lost their wrapper and set-up functions and
    their hand-made cursors and commits; a private file is served after
    one lookup instead of two, and the admin's user actions look each
    account up once.
  - Front end: the pages share one header, one set of card and button
    styles, and the sign-in pages one layout; the gallery lightbox and
    the feed share one click-to-zoom (`MI.zoom` in `common.js`); the
    lightbox deletes through the card's delete and uses the media URL
    the server sends; the gallery's two card builders (the first page's
    template and the cards loaded while scrolling) match again, down to
    "No tags" and "Edit tags".
  - Dead code removed.

### Removed

- The startup code that upgraded databases created before 1.0.0 (added
  columns, rewritten foreign keys, a fill-in of each item's stored type).
  A database that has run 1.0.0 already has all of it; a fresh database
  gets the same schema as before. To upgrade from an earlier version, run
  1.0.0 once first.
- `/api/feed` no longer reads the old single `type` parameter, only
  `types` (the feed page and `docs/api.md` already used only `types`).
- `FLASK_APP` is no longer set in the image or the example compose;
  nothing used it. A stack that copied it can drop the line. For the
  Flask command line, use `flask --app run:app`.

### Fixed

- A file that disappeared between the app's check and its delete (for
  example a queued file deleted over WebDAV while it was being rejected
  in the queue) made the request fail with a server error. Deleting
  something that is already gone is no longer an error.
- The Telegram bot gave no answer at all when the index's reply to its
  account lookup was unreadable; it logged an unhandled error. It now
  tells the sender it can't reach the index right now, as for any other
  outage.
- `scripts/set_password_hash.py` accepted `plain` hashes, which the
  pinned Werkzeug can no longer check: every login of that user then
  failed with a server error. It accepts only `scrypt` and `pbkdf2` now.
- Tag suggestions on the Untagged page stopped after the first comma. The
  page now uses the same tag autocomplete as every other page.
- The tag count on the Tags page read "12 von 40 Tags" while filtering;
  it reads "12 of 40 Tags".
- On phones, the Tags and Upload pages padded the top bar differently from
  every other page.

### Security

- The Discord bot now checks every link it downloads, attachments and
  embeds included, against the same public-address rule as the bots'
  other link paths, one redirect hop at a time. Before, a link ending in a
  media extension was fetched without it, so a member with the ingest
  role could make the bot fetch a service inside the stack.

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

[Unreleased]: https://github.com/Houdini99/MediaIndex/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/Houdini99/MediaIndex/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Houdini99/MediaIndex/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Houdini99/MediaIndex/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Houdini99/MediaIndex/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Houdini99/MediaIndex/releases/tag/v1.0.0
