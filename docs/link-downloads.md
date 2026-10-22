# Link downloads

Both bots can index what a link points at, not just files sent to them
directly. This page describes how a link becomes a file. It is the same
code in both bots (`bots_common/`), with the differences noted. For the
settings, see [Configuration](configuration.md#discord-bot-discord-bot).

**Contents**

- [How a link is handled](#how-a-link-is-handled)
- [Video sites (yt-dlp)](#video-sites-yt-dlp)
- [Site logins](#site-logins)
- [YouTube PO tokens](#youtube-po-tokens)
- [Limits](#limits)
- [Which addresses are fetched](#which-addresses-are-fetched)
- [When yt-dlp breaks for a site](#when-yt-dlp-breaks-for-a-site)

## How a link is handled

1. **A link that names a media file** (`…/cat.gif`, or a
   `…?format=jpg`-style link) is downloaded directly.
2. **A link a yt-dlp extractor claims** (YouTube, TikTok, Reddit, X and the
   other sites yt-dlp supports) is downloaded with yt-dlp.
3. **Any other link** is asked what it serves, with one `HEAD` request, the
   way Discord's own link previews work. If the answer is an image or video
   type the index accepts, it is downloaded directly, under the filename the
   server offers if it offers one. This is how CDN links such as
   `https://files.example/ab/cd/<md5>`, which name no file, are recognised.
   At most 4 links per message are asked, each with a 15-second timeout.
4. **What is left**:
   - The **Discord bot** checks the page for a video with yt-dlp's generic
     extractor, fetching metadata only, and downloads it if it finds exactly
     one. A page with several videos is refused rather than guessed at. At
     most `DISCORD_YTDLP_MAX_PROBES` links per message are checked.
   - The **Telegram bot** refuses the link.

Article pages and other links without media end up refused, with a 🚫
in Discord or a reply in Telegram.

## Video sites (yt-dlp)

- Separate video and audio streams are merged into one MP4 with ffmpeg.
- The site the video came from is added as a tag (`youtube`, `tiktok`, …),
  next to any tags in the message.
- The resolution is capped at `*_YTDLP_QUALITY` (1080 by default). The cap
  is a preference, not a filter: a vertical 1080p video still qualifies.
  H.264 is preferred at every height, so the file plays in every browser.
- A site that refuses non-browser clients with HTTP 403 is tried once more
  with a Chrome browser fingerprint.
- Some sites sign a download link for whoever loaded the page, so a copied
  download link fails from the server. For those, the bots download from
  the video page instead (see `bots_common/parsing.py`).
- Progress is written to the container log: per-stream progress, the
  merge, and the final size.
- When yt-dlp reports a link as having nothing to download (*no video in
  this post*, *video unavailable*, a private video), the link is answered
  with 🚫 and not retried. Real failures are logged with their details,
  and in Discord [retried](discord-bot.md#retries).

The bot images install the newest yt-dlp when they are built; run
`./build.sh` again to update it.

## Site logins

YouTube, Instagram, X, Facebook and Reddit hide much of their content
behind a login. Without one, yt-dlp gets "Sign in to confirm you're not a
bot", "login required" or HTTP 403. An admin can store a login for each of
these sites, plus one for *Other sites*, on **Admin → Site logins**; see
[Admin](admin.md#site-logins).

A login is a Netscape-format `cookies.txt` exported from a browser that is
signed in to the site. How the bots use it:

- Right before resolving a link, the bot asks the index for the login of
  that link's site (or the *Other sites* login). yt-dlp gets a private
  copy for that one download, deleted afterwards.
- After a download that used the login and worked, the bot sends back the
  cookies the site changed, so the session stays alive. The index refuses
  that update if an admin replaced or removed the login in the meantime,
  or if it would log the site out.
- If a YouTube link fails with the stored login for a login or format
  reason, it is tried once more without the login, through the PO-token
  path below.
- If the index can't be reached, the link is reported as failed and, in
  Discord, retried. It is never downloaded without the login instead.
- A failure caused by the login says so, and names the admin page to fix
  it.

**Exporting a login:** use a spare account, export the cookies from a
private browser window, and close that window right after. A browser that
keeps using the session changes its cookies and ends the copy you
uploaded.

A mounted `cookies.txt` (`*_YTDLP_COOKIES`) can still be used as a fallback
for sites without a stored login.

## YouTube PO tokens

YouTube asks servers to prove they aren't bots. The `bgutil` service in
the stack answers that challenge, and the bot images carry its yt-dlp
plugin. With it, most public YouTube videos download without any login.

The plugin in the bot images and the `bgutil` server must have the same
major version, or YouTube downloads silently go without PO tokens. Both
are pinned (the server by digest in the compose file, the plugin in
`bots_common/Dockerfile`); update them together. The Discord bot's
startup line says whether PO tokens are on.

`*_YTDLP_POT_PROVIDER` set to an empty value turns this off.

## Limits

| Limit | Discord | Telegram |
|---|---|---|
| Direct file links | `DISCORD_LINK_MAX_MB` (512 MB) | `TELEGRAM_LINK_MAX_MB` (512 MB) |
| yt-dlp downloads | `DISCORD_YTDLP_MAX_MB` (512 MB) | `TELEGRAM_YTDLP_MAX_MB` (512 MB) |
| No progress for | `DISCORD_YTDLP_STALL_TIMEOUT` (120 s) | `TELEGRAM_YTDLP_STALL_TIMEOUT` (120 s) |
| Not downloading yet after | `DISCORD_YTDLP_MAX_DURATION` (1800 s) | `TELEGRAM_YTDLP_MAX_DURATION` (1800 s) |

A download is only stopped when it stops making progress, never for being
long, so a large file on a slow but working connection finishes. Size
limits are checked before and during the download.

## Which addresses are fetched

The bots run inside the stack's networks, so a link must not be able to
point them at the web app, the database or anything else on the server.

- Before the `HEAD` request in step 3, the generic-extractor check in step
  4, and every direct download (including the Discord bot's attachments
  and embeds), the host is looked up and refused unless all its addresses
  are public. Loopback, private ranges, link-local addresses such as
  `169.254.169.254`, and IPv6 unique-local addresses are refused.
- Redirects are followed one hop at a time, and every hop is checked again.
- Only members with the Discord ingest role, and only linked users in
  Telegram, can make a bot fetch anything at all.

See [Security](security.md#known-limitations) for what this doesn't cover.

## When yt-dlp breaks for a site

Sites change their pages, and yt-dlp's extractor for a site sometimes
stops working until yt-dlp releases a fix. `bots_common/ytdlp_fixes.py`
holds small stopgaps for such cases. A stopgap only steps in when yt-dlp's
own extractor finds no video, so it goes quiet by itself once yt-dlp is
fixed. Each time it is used it logs `ytdlp_fixes: … stepped in`; when that
line no longer appears after a yt-dlp update, the stopgap can be removed.
