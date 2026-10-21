# Admin panel

Admins see **Admin** in the navigation bar (`/admin`). Its tabs are
**Panel**, **Integrity**, **Duplicates**, **Site logins** and **Logs**.
Admins can also edit tags on and delete anyone's media, directly in the
gallery and the feed.

**Contents**

- [Panel](#panel)
- [Tags](#tags)
- [Integrity](#integrity)
- [Duplicate finder](#duplicate-finder)
- [Site logins](#site-logins)
- [Logs](#logs)
- [Maintenance scripts](#maintenance-scripts)

## Panel

**Overview:** numbers of users, media and tags, storage used and free disk
space, and the banned and watched IP addresses.

**Users:**

- **Create** accounts. Registration is closed by default, so this is how
  people get one. MediaIndex is built for [one to five
  people](../README.md#who-its-for) who know each other. Usernames are 1
  to 32 characters: letters, digits, `_`, `.` and `-`. You choose the
  first password; the user replaces it with their own when they first log
  in, and can't use the index before that.
- **Reset a password.** This also logs the user out on every device, and
  at their next login they replace the password you set with their own.
  Resetting your own password doesn't ask you again.
- **Make or remove admins.** You can't change your own admin status, so
  there is always at least one admin.
- **Delete a user.** You choose what happens to their media: delete it, or
  give it to another user. Private files given to someone else are
  re-encrypted with the new owner's key.

**IP bans:** the addresses banned automatically after 10 failed logins,
and the addresses that are close to it. You can lift a ban, or ban an
address yourself. Automatic bans expire after `BAN_DURATION_HOURS` (24);
bans you set never expire. See [Security](security.md#logins-and-bans).

## Tags

**Rename** a tag across the whole library, or **remove** it everywhere.
Both have a dry run that shows what would change before anything does.
Admins also get rename and remove buttons next to each tag on the
[Tags page](using.md#tags).

## Integrity

**Integrity** (`/admin/integrity`) compares the database with the files on
disk and lists:

- **missing files:** items whose media file is gone;
- **orphaned files:** media files no item points to;
- **orphaned thumbnails:** thumbnails no item points to;
- **placeholder thumbnails:** items still showing the placeholder, for
  example because the server restarted while their thumbnail was being
  made, or because the file can't be decoded.

Each list has a one-click fix: remove the items, delete the files, or
render the thumbnails again. Files less than an hour old are never treated
as orphans, because an upload in progress looks like one for a moment.

## Duplicate finder

Uploads with exactly the same bytes are rejected automatically. The
duplicate finder (`/admin/duplicates`) finds the rest: the same picture
saved at another quality, a screenshot of a video, a re-encoded copy from
another source.

**Scanning.** A scan computes three perceptual fingerprints (pHash, dHash
and aHash) for every public item. Images and GIFs are fingerprinted from
their thumbnail, which is fast, or from the original file, which is more
precise; you choose per scan. Videos are always sampled frame by frame
from the file, so a re-encoded copy is recognised even if its first frame
differs.

- The scan runs in the background with a progress bar, an estimated time
  and a **Cancel** button, which takes effect even in the middle of a
  video.
- It only fingerprints what is new since the last scan.
- A video that takes longer than `DUPE_ITEM_TIMEOUT` (180 seconds) to
  decode is stopped and listed as *could not be fingerprinted*; the scan
  carries on with the next item.
- **Private media is never scanned**, and private thumbnails are never
  shown.

**Matching.** Choose the fingerprint (pHash, dHash, aHash, or pHash and
dHash together) and how many bits may differ. Groups update immediately,
without scanning again. Because a video is compared frame by frame, an
image that is a frame of a video lands in the video's group, and a short
clip that is part of a longer one is shown as a *subset match*.

**Groups** show the items side by side with resolution, file size,
uploader, date and tags. The best copy is marked with ★: the most pixels,
then the largest file, then the most tags, then the oldest. You can pick
another one. Sort by similarity, space you would free, or group size;
show only groups with more than one uploader; or export everything as
CSV.

**Resolving takes two steps, and the first can be undone.**

1. **Resolve** keeps the chosen copy and moves the others to the
   **quarantine**. They are tagged `dupe` and `dupe-of:<id>`, and their old
   tags are saved, so a restore puts them back exactly. You can merge the
   other copies' tags onto the one you keep first, so no tagging work is
   lost.
2. **Purge** deletes what is in the quarantine, files and thumbnails
   included. Nothing is deleted before you purge.

**Not duplicates** hides a group for good. Hidden groups are listed and
can be brought back.

## Site logins

**Site logins** (`/admin/site-logins`) holds the logins the bots use to
download from YouTube, Instagram, X/Twitter, Facebook and Reddit, plus an
**Other sites** login used for every other site. [Link
downloads](link-downloads.md#site-logins) explains how the bots use them.

- **Upload** a Netscape-format `cookies.txt` (at most 1 MB) for a site.
  Only the cookies of that site's own domains are kept, so an export of the
  whole browser filed under YouTube keeps YouTube's cookies and nothing
  else. *Other sites* keeps everything.
- Each site shows its status: logged in, no login cookie found, expired,
  or unreadable. It also shows who uploaded the login and when the bots
  last refreshed it.
- **Remove** deletes a login.

Logins are stored encrypted with a key derived from `SECRET_KEY`, so a
database backup holds no usable session. After `SECRET_KEY` changes, the
logins show as *unreadable* until they are uploaded again.

## Logs

**Logs** (`/admin/logs`) shows the end of the current log file, an older
one, or one day of the security log, with a filter for the number of lines
and for text. See [Operations](operations.md#logs) for what the application
log contains, and [Security log](operations.md#security-log) for how to
read the security log.

## Maintenance scripts

The scripts in [`scripts/`](../scripts/) do from the command line what the
admin panel does, plus a few repairs. The database is only reachable
inside the stack, so run them in the web container:

```bash
docker exec -it mediaindex-web python scripts/<script> <arguments>
```

| Script | What it does |
|---|---|
| `set_password.py <username>` | Sets a user's password. It asks for the password, so it never appears on the command line. Logs the user out everywhere. Unlike a reset on the admin panel, it doesn't make the user choose a new password. |
| `set_password_hash.py <username>` | Stores a ready-made Werkzeug password hash, read from standard input. |
| `rename_tag.py <old> <new> [--dry-run]` | Renames a tag everywhere. |
| `remove_tag.py <tag> [--dry-run]` | Removes a tag everywhere. |
| `normalize_tags.py [--dry-run]` | Rewrites tag strings that aren't in the stored form (for example with spaces around the commas), so every tag can be found. It changes only rows that need it, and never reorders or adds a tag. |
| `regen_image_thumbs.py [--dry-run] [--all]` | Renders image thumbnails again: by default those of photos with an EXIF rotation, with `--all` every image, private ones included. Each new thumbnail gets a new file name, so browsers don't keep showing the old one. |

Run a script with `--dry-run` first where it has one.
