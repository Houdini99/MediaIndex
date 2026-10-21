# Discord bot

The Discord bot connects the index with one Discord server, in both
directions:

- **Announcements:** every new public item is posted to a channel.
- **Ingest:** media posted in another channel by members with a chosen
  role is added to the index.
- **Private ingest by DM:** those members can also DM the bot, and what
  they send is stored as their private media.

It runs as the `discord-bot` container in the same stack. Its variables
are listed in [Configuration](configuration.md#discord-bot-discord-bot).

**Contents**

- [Setup](#setup)
- [Linking accounts](#linking-accounts)
- [Announcements](#announcements)
- [Ingest](#ingest)
- [Private ingest by DM](#private-ingest-by-dm)
- [Reactions and the bot-logs channel](#reactions-and-the-bot-logs-channel)
- [Retries](#retries)
- [How the bot talks to the index](#how-the-bot-talks-to-the-index)
- [Checking it works](#checking-it-works)

## Setup

1. **Create the bot.** In the [Discord Developer
   Portal](https://discord.com/developers/applications), create an
   application and add a bot. Copy its token into `DISCORD_BOT_TOKEN`, and
   turn on the **Message Content Intent**; without it, messages arrive
   empty.
2. **Invite it** to your server with the *Send Messages*, *Attach Files*,
   *Add Reactions* and *Read Message History* permissions.
3. **Pick the channels and the role.** Use a server, or channels, that
   only the people who share your index can read: announcements carry the
   media itself. In Discord, turn on Developer Mode (User Settings →
   Advanced), then right-click to copy IDs:
   - the announcement channel → `DISCORD_NOTIFY_CHANNEL_ID`;
   - the ingest channel → `DISCORD_INGEST_CHANNEL_ID`;
   - the role whose members may add media → `DISCORD_INGEST_ROLE`. Use the
     role's ID rather than its name.
   - optionally, a channel for status lines → `DISCORD_LOG_CHANNEL_ID`.
4. **Give it an index account.** Create a user for the bot on **Admin →
   Users** (by default named `Bot`; otherwise set `INDEX_BOT_USERNAME`).
   Log in as that user, give it a new password when asked (the bot never
   uses it), and generate an app password on **Untagged** (`/queue`); put
   it in `INDEX_BOT_APP_PASSWORD`. The account must be
   listed in `DELEGATED_UPLOADERS` on the web app, which it is by default.
5. **Set `INDEX_PUBLIC_URL`** to your index's public address, for the
   links in announcements.
6. **Build and start it.**

   ```bash
   ./build.sh discord
   docker compose up -d discord-bot
   ```

The bot needs no published port and no reverse-proxy route. It mounts
`thumbnails/` and `media_files/` read-only, to attach files to
announcements.

## Linking accounts

Each user links their own Discord account, so that what they post is
filed under their index account:

1. On the index, open **Settings** (the cogwheel) → **Connect Discord** →
   **Get link code**. The page shows a line like `link ABCD-EFGH`.
2. Send that line to the bot as a direct message within 10 minutes.
3. The bot confirms, and the settings page updates by itself.

The bot redeems the code with the sender's ID as Discord reports it, so a
link proves that one person controls both accounts. Nobody can link
someone else's Discord account. Codes are stored only as a hash, work
once, and are accepted only by DM: posted in a channel, anyone could
redeem a code first. Redeeming a code moves a Discord account's link from
any other index account.

Linking works for everyone who can DM the bot, with or without the ingest
role, and also when DM ingest is turned off.

**Ownership:** media posted by a linked member belongs to their index
account, gets their `user:<name>` tag, and can be edited and deleted by
them. Media from unlinked members belongs to the bot's account.

## Announcements

Every new **public** item is posted to the announcement channel as an
embed: an icon and colour for the type (🖼️ image, 🎞️ GIF, 🎬 video),
the tags as links to a gallery search, the uploader as a link to their
media, and the type and size in the footer.

- The original file is attached when it fits the server's upload limit
  (10 MB on a server without boosts), and the thumbnail otherwise, with a
  link to the index. `DISCORD_NOTIFY_THUMBNAIL_ONLY=true` always attaches
  the thumbnail.
- A video is announced once its real thumbnail exists.
- Private media is never announced.
- New items are queued in Redis, up to 1000, so items added while the bot
  is down are announced when it comes back.
- Links in announcements need a login to the index to open, but the
  attached file or thumbnail doesn't: anyone who can read the channel
  sees it.

## Ingest

The bot reads messages in the ingest channel from members holding
`DISCORD_INGEST_ROLE`, and ignores everyone else. It picks up:

- **attachments**, including in forwarded messages;
- **links in the text** — see [Link downloads](link-downloads.md) for how
  a link becomes a file, including video sites through yt-dlp;
- **media behind embeds**, such as GIFs from Discord's GIF picker and
  image links Discord previews. If an embed isn't there yet, the bot looks
  at the message once more a few seconds later.

Large files are streamed to disk, not held in memory. Discord's file links
expire; when one has, the bot asks Discord for a fresh link and tries
again.

**Tags.** Every item gets `DISCORD_INGEST_TAGS` (`discord`). If the
message text looks like a tag list (`cats,funny`, or a single word without
spaces), those tags are added to every item in the message. Links are
removed from the text before this check, so `cats,funny https://…` tags
the linked media. Ordinary messages are not mistaken for tags: anything on
several lines, longer than 200 characters, with mentions or custom emoji,
or a sentence with spaces and no commas, is left alone.

**Forwards** can't carry text, so Discord delivers your tags as a separate
message. An untagged forward waits `DISCORD_FORWARD_TAG_WAIT` seconds (15)
for a tag message from the same person. When one arrives, the bot reacts
🏷️, tags all of that person's waiting forwards with it, and indexes them;
otherwise they are indexed with the default tags when the time is up.

Everything added from the channel is public.

## Private ingest by DM

Members holding the ingest role can send media to the bot in a direct
message instead. Everything works as in the channel (attachments, links,
forwards, tags, retries, reactions), but:

- the media is stored as **private** media of the sender's index account,
  encrypted, visible to nobody else, and never announced;
- status lines go back into the DM instead of the bot-logs channel, so the
  shared channel never learns what someone filed privately;
- the sender must have [linked their account](#linking-accounts). An
  unlinked sender is told how to link, and nothing is downloaded;
- the role is checked in the ingest channel's server on every DM, so
  removing someone's role stops their DMs at once.

A file that is already in the index is reported as a duplicate; it isn't
copied or made private. With `DISCORD_INGEST_ROLE` unset, DMs are refused.
`DISCORD_DM_INGEST=false` turns DM ingest off.

## Reactions and the bot-logs channel

The bot reacts to each message it handles:

| Reaction | Meaning |
|---|---|
| ✅ | added |
| ♻️ | already in the index |
| 🚫 | nothing that can be indexed (unsupported type, no video on the page, too large) |
| ❌ | failed |
| ⏳ | the index refused the bot's password; the bot is waiting before it tries again |
| ⬇️ | a video download is running |
| 🔁 | queued for a retry |
| 🏷️ | this message's tags were applied to waiting forwards |

With `DISCORD_LOG_CHANNEL_ID` set, the bot also posts a line for each
result in that channel, linking back to the message: what was added, with
its owner and tags; the reason for a 🚫 or ❌; and retries with their
delay and attempt number. A duplicate line links the item it duplicates,
with its owner and tags, unless that item is someone's private media, in
which case it only says so. The channel also gets a status line when the
bot starts.

## Retries

An ingest that fails for a reason that may pass is queued in Redis and
tried again: the index refusing or not answering, a network error during
the transfer, a server error from the source or the index, or a stalled
download. Retries wait 60 seconds, then twice as long each time, up to an
hour, for 5 attempts. The message shows 🔁 while it waits, and the queue
survives a restart of the bot.

Answers that won't change are not retried: an unsupported file type, a
video that is too large, a page without a video, a site that blocks the
server, or a link that no longer exists.

A retry handles the whole message again. Anything that already made it in
comes back as ♻️ duplicate, which costs nothing.

`DISCORD_INGEST_RETRY=false` turns retries off; the other
`DISCORD_INGEST_RETRY_*` variables tune them.

## How the bot talks to the index

The bot never touches the database. It uses the web app's API over the
stack's internal network (see [API](api.md#bot-endpoints)):

- uploads go to `POST /api/upload`, signed in with the bot account's app
  password, naming the linked user they are for;
- `GET /api/discord/user` answers which index account a Discord user is
  linked to (cached for `DISCORD_LOOKUP_TTL` seconds), and
  `POST /api/discord/verify` redeems link codes.

These calls are only accepted from accounts listed in
`DELEGATED_UPLOADERS`. If the index can't be asked who posted something,
the ingest is retried; it is never filed under the bot account instead.

If the index refuses the bot's app password, the bot stops calling it for
`AUTH_RETRY_SECONDS` (15 minutes). Ten failed sign-ins would get the bot's
container banned for a day.

## Checking it works

When it starts, the bot posts a status line to the bot-logs channel (if
you set one). It should contain `ingest: @<your role> only`; `⚠️ OPEN TO
EVERYONE` means `DISCORD_INGEST_ROLE` didn't reach the container. The
same line shows whether DMs, the link resolver, site logins, PO tokens and
retries are on.

Without a bot-logs channel, check the container log. A missing role shows
up there as a warning starting with `ingest: DISCORD_INGEST_ROLE is not
set`.

```bash
docker logs mediaindex-discord-bot
```
