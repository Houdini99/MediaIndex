# Using MediaIndex

Everything here needs an account; an admin creates it for you. The first
time you log in, and after an admin resets your password, you replace the
password the admin gave you with one of your own. The navigation bar has
**Gallery**, **Feed**, **Upload**, **Untagged** (the [WebDAV
inbox](webdav.md)) and **Tags**, plus the cogwheel for your **Settings**.
Admins also see **Admin** ([Admin panel](admin.md)).

**Contents**

- [Gallery](#gallery)
- [Feed](#feed)
- [Tags](#tags)
- [Upload](#upload)
- [Private media](#private-media)
- [Who can change what](#who-can-change-what)
- [Settings: password, Telegram and Discord](#settings-password-telegram-and-discord)

## Gallery

The gallery (`/`) is a grid of thumbnails that loads more as you scroll.

**Filters** in the toolbar:

- **Search** looks for text inside the tags, ignoring case, so `cat` also
  finds `cats`. Separate several terms with commas to require all of
  them: `cats, sunset` finds media matching both.
- **Exclude** hides media carrying any of the tags you list. It compares
  whole tags: excluding `ai` doesn't hide `rain`.
- **Type**: all, images, videos or GIFs.
- **Sort**: newest first, oldest first, or random. A random order keeps
  its shuffle while you scroll, so nothing repeats.
- **Hide AI** hides media tagged `fake`, `ai`, `deepfake`, `grok`, `gemini`
  or `chatgpt`. Your admin can change that list.
- **Private only** shows just your own [private media](#private-media).

The tag boxes suggest existing tags as you type, completing the tag after
the last comma.

**Each card** shows the type, and a *private* badge on your private items.
You can edit its tags in place or delete it. Click it to open the full-size
view, which plays videos, downloads the file under its original name, and
closes with `Escape`.

**Select mode** works on many items at once. Turn on **Select** in the
toolbar, click cards (or **Select all**), then use the action bar to
delete them, add or remove tags, or make them private or public.

## Feed

The feed (`/feed`) shows one item per screen, scrolling vertically. Its
order is shuffled; when you reach the end it shuffles again and keeps
going.

- **Videos** play when they scroll into view and pause when they leave.
  Drag the progress bar to seek, click to mute, or use the volume slider.
- **Each slide** has download and delete buttons, the uploader, and the
  tags.
- **Settings** (the gear in the feed toolbar) choose which types to show,
  tags an item must all carry, and tags that hide it. Both compare whole
  tags. They are saved in your browser,
  together with volume and mute. **Reset filters** clears the filters only.
- **Slideshow** advances by itself: images and GIFs after a set time (1 to
  120 seconds, 5 by default), videos when they finish.
- **Fullscreen** hides everything but the media. It isn't remembered when
  you reload.

| Key | Action |
|---|---|
| `↓` or `j` | next item |
| `↑` or `k` | previous item |
| `→` / `←` | skip a video 5 seconds forward / back |
| `m` | mute or unmute |
| `s` | start or stop the slideshow |
| `f` | fullscreen on or off |
| `Escape` | close the settings, or leave fullscreen |

## Tags

The tags page (`/tags`) lists every tag with how many items carry it. Other
users' private media isn't counted. Type to filter the list, and click a
tag to search the gallery for it. Admins can rename or remove a tag here;
see [Admin](admin.md#tags).

Every item is tagged automatically with its type (`image`, `gif` or
`video`) and its owner (`user:<name>`). A tag is the text between two
commas, so `alpha, beta` is stored as the two tags `alpha` and `beta`.

## Upload

The upload page (`/upload`) takes several files at once, dropped onto the
page or picked from a dialog. Each file shows a preview, and gets its own
tags (with suggestions) and its own **private** checkbox.

- Accepted types are JPG, PNG, WebP, GIF, MP4, WebM, AVI, MOV and MKV, up
  to 10 GiB per upload. AVI, MOV and MKV may not play in every browser.
- Files are identified by their content. Uploading a file that is already
  in the index is reported as a duplicate and skipped.
- After the upload, a summary lists what was added, what was a duplicate
  and what failed.
- Video thumbnails are made in the background, so a new video shows a
  placeholder for a few moments.

Other ways in: the [WebDAV inbox](webdav.md), the [Discord
bot](discord-bot.md) and the [Telegram bot](telegram-bot.md).

## Private media

MediaIndex is one library shared by a small private group of one to five
people. Every item is either public or private:

- **Public** means visible to everyone with an account on your index. It
  doesn't mean the internet: nothing can be seen without logging in. If
  your index runs the Discord bot, public items are also posted to its
  announcement channel.
- **Private** means visible only to you.

An item marked private doesn't show up for anyone else in the gallery, the
feed, the tag counts, search results or the API, and its file can't be
opened by anyone else even with a direct link. Private items are never
announced in Discord.

Private files and their thumbnails are encrypted on disk with a key that
belongs to your account. Videos still stream and seek normally.

You can change your mind after uploading: in select mode, **Make private**
encrypts the selected items and **Make public** decrypts them.

## Who can change what

You can edit tags on, delete, and change the privacy of the items you
own. Admins can edit and delete anyone's items; when an admin makes
someone's item private, it is encrypted with the owner's key.

## Settings: password, Telegram and Discord

The **Settings** page (the cogwheel in the navigation bar) changes your
password and connects your chat accounts, so that what you send the bots
is filed under your account.

- **Password.** Enter your current password and the new one twice; it
  needs at least 8 characters. Changing it logs you out on every other
  device. Your app password stays the same.
- **Connect Telegram.** Send the Telegram bot `/start`; it replies with
  your numeric Telegram ID. Paste that into the form and save. An
  @username works too, but the numeric ID is safer: see [Telegram
  bot](telegram-bot.md#linking).
- **Connect Discord.** Click **Get link code**. The page shows a line like
  `link ABCD-EFGH`; send it to the Discord bot as a direct message within
  10 minutes. The page notices by itself when the bot has linked you. See
  [Discord bot](discord-bot.md#linking-accounts).

Either link can be removed with **Unlink**.

The WebDAV app password, which the WebDAV inbox and the bots use, is on
the **Untagged** page; see [WebDAV inbox](webdav.md#app-password).
