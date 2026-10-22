# Telegram bot

Send the Telegram bot a photo, a video or a link in a private chat, and it
is added to the index under **your** account. It only works for users who
have linked their Telegram account; everything from anyone else is
ignored before anything is downloaded.

It runs as the `telegram-bot` container, together with a local
`telegram-bot-api` server so files up to 2000 MB can be indexed. The bot
polls Telegram for messages, so it needs no published port and no
reverse-proxy route. Its variables are listed in
[Configuration](configuration.md#telegram-bot-telegram-bot).

**Contents**

- [Setup](#setup)
- [Linking](#linking)
- [Sending media and tags](#sending-media-and-tags)
- [Links](#links)
- [Commands](#commands)
- [File size and the local Bot API server](#file-size-and-the-local-bot-api-server)
- [Troubleshooting](#troubleshooting)

## Setup

1. **Create the bot.** Message [@BotFather](https://t.me/BotFather), send
   `/newbot`, and put the token in `TELEGRAM_BOT_TOKEN`. Put its @handle in
   `TELEGRAM_BOT_NAME` on the web app, so the settings page can tell users
   whom to message.
2. **Give it an index account.** The bot signs in as `INDEX_BOT_USERNAME`
   (`Bot`) with that account's app password in `INDEX_BOT_APP_PASSWORD`,
   generated on **Untagged** (`/queue`) while logged in as that account.
   The first login asks for a new password for the account; the bot never
   uses it. The Discord bot can use the same account and app password.
3. **List that account in `DELEGATED_UPLOADERS`** on the web app (the
   default, `Bot`, already does). Without it, the bot can't upload for
   anyone and can't look up who a sender is, so nothing is indexed.
4. **Set up the local Bot API server:**
   - Get an api_id and api_hash at [my.telegram.org](https://my.telegram.org)
     → *API development tools*, and set `TELEGRAM_API_ID` and
     `TELEGRAM_API_HASH`. They identify the server, not the bot.
   - Log the bot out of Telegram's cloud API, once, before the first start:

     ```bash
     curl -s "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logOut"
     ```

     A local server can't take over a bot that is still signed in to the
     cloud API, and the error you would get is a bare 401.
5. **Build and start it.** Every image builds from the repository root,
   and `./build.sh` picks the right Dockerfile.

   ```bash
   ./build.sh telegram
   docker compose up -d telegram-bot-api telegram-bot
   ```

6. **Each user links their account** on the index; see [Linking](#linking).

## Linking

On the index, open **Settings** (the cogwheel) → **Connect Telegram** and
enter your Telegram account. Send the bot `/start` first: it replies with
your numeric ID, ready to paste.

The form also takes an @username, but the numeric ID is safer. A username
can be given up and then registered by someone else, whose messages would
then be filed under your account. The numeric ID never changes. So a
username is only used to find you **once**: the first time it matches,
your numeric ID is saved, and from then on only the ID counts. Changing
the username in the form clears the saved ID, so you can link again on
purpose. Two index accounts can't link the same Telegram account.

The bot asks the index who a sender is (`GET /api/telegram/user`); it has
no access to the database.

## Sending media and tags

**Tags come from the caption.** A caption counts as tags if it is one line
of at most 200 characters, without links or mentions, and is either
comma-separated or a single word: `cats,funny` is tags, `look at this lol`
is not. `TELEGRAM_INGEST_TAGS` (`telegram`) is added to everything.

**Nothing untagged is indexed.** Media sent without tags is held, and the
bot asks for them. Then either:

- **reply** to the item (or to the bot's question) with tags, to tag just
  that item; or
- **send a tag list** on its own, to tag everything you have waiting. Send
  ten photos, then type `holiday,beach`.

Waiting items are forgotten after `TELEGRAM_PENDING_TTL` (a day).
`/pending` lists them and `/cancel` drops them. The bot only remembers
Telegram's reference to the file, not the file itself, so nothing is
downloaded for an item you never tag.

**Albums** count as one: Telegram puts the caption on only one item of an
album, so the bot collects the album for a moment and applies the caption
to every item in it.

**Private:** `/private` makes what you send next
[private](using.md#private-media); `/public` switches back. Public items
are announced in Discord like any other upload; private ones are not.

## Links

Send a link, and the bot downloads what it points at: directly for a link
to a file, through yt-dlp for video sites. [Link
downloads](link-downloads.md) describes how links are handled, logged in
and limited. A page that no yt-dlp extractor recognises and that doesn't
serve a file is refused.

The tag rules are the same as for media: `cats,funny https://…` is indexed
right away, while a bare link is held until it gets tags, and nothing is
fetched until then. Several links in one message are held separately.
While a download runs, the bot shows "⬇️ Downloading…".

## Commands

| Command | Effect |
|---|---|
| `/start` | Whether you are linked, and your numeric ID to paste on the settings page if not |
| *(media or a link)* | Index it |
| `/private` | Save what you send next as private |
| `/public` | Back to public |
| `/pending` | List what is waiting for tags |
| `/cancel` | Forget what is waiting |

The bot only works in private chats.

## File size and the local Bot API server

Telegram's cloud API won't hand a bot any file larger than 20 MB. The
stack therefore runs its own Bot API server, `telegram-bot-api`, which
raises the limit to 2000 MB. In this mode Telegram writes each file into
the server's work directory, and the bot reads it from there instead of
downloading it.

Two parts of the compose file make that work:

- **The work directory is one volume, mounted at the same path in both
  containers** (`/var/lib/telegram-bot-api`). If the paths differ, the
  bot can't find the files. It checks the directory at startup and each
  file before use, and says what is wrong.
- **The bot runs as user `101:101`**, the user the Bot API server runs
  as. The server doesn't clean up its work directory; the bot deletes
  each file after uploading it, which it can only do as that user.

The work directory's folder names contain the bot token, so treat the
volume like any other secret.

**To use the cloud API instead,** set `TELEGRAM_API_BASE` to an empty
value and remove the `telegram-bot-api` service. Files over 20 MB are then
answered with a message pointing to the [WebDAV inbox](webdav.md).

## Troubleshooting

| Symptom in the bot's log | Cause |
|---|---|
| `gunicorn … RuntimeError: SECRET_KEY environment variable is not set` | The image tagged `index-telegram-bot` is the web app, built from the wrong Dockerfile. Rebuild with `./build.sh telegram`. `docker image inspect index-telegram-bot:latest --format '{{.Config.Env}}'` should list `BOT_MODULE=telegram_bot.bot`. |
| `RuntimeError: TELEGRAM_BOT_TOKEN environment variable is not set` | The token is unset. Only this container stops; the rest of the stack keeps running. |
| `the index rejected the app password … DELEGATED_UPLOADERS` | The bot's account isn't in `DELEGATED_UPLOADERS`, or its app password is wrong. |
| The bot answers `/start` but indexes nothing | The sender isn't linked on the settings page, or is linked to another index account. |
| `error: environment variable TELEGRAM_API_ID is required` | The Bot API server has no api_id and api_hash. |
| `401 Unauthorized` from the Bot API server | The bot was never logged out of the cloud API; run the `logOut` call from the setup, once. |
| `… is not mounted in this container` | The `telegram_api_data` volume is missing from the bot, or mounted at a different path than in `telegram-bot-api`. |
| `… is not writable by uid 0`, or files pile up in the volume | The bot isn't running as user `101:101`. |

Some behaviour that can look like a fault:

- An unlinked sender is told why nothing happens at most once an hour.
- If the index can't be reached, the bot says so, rather than telling a
  linked user that they aren't linked. Nothing is downloaded in the
  meantime.
- If the index refuses the bot's app password, the bot stops calling it
  for `AUTH_RETRY_SECONDS` (15 minutes), so it never gets its container
  banned.
