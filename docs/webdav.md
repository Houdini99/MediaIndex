# WebDAV inbox

Every user gets a personal network folder. Copy media into it from your
computer or phone, then tag it on the **Untagged** page and save it to the
index. Nothing in the folder is indexed until you do.

**Contents**

- [App password](#app-password)
- [Connect a client](#connect-a-client)
- [The Untagged queue](#the-untagged-queue)
- [How the inbox works](#how-the-inbox-works)

## App password

WebDAV doesn't use your login password. Open **Untagged** (`/queue`) and
click **Generate password**. The password is shown once; copy it then.
**Regenerate password** replaces it, and **Revoke** turns WebDAV access off.
The page also shows the address and your username.

Failed WebDAV sign-ins count toward the same [IP ban](security.md#logins-and-bans)
as failed logins on the website.

The same app password is what the bots and other API clients use to sign
in as your account; see [API](api.md#authentication).

## Connect a client

The address is `https://<your-domain>/dav/`; sign in with your index
username and the app password.

| System | How |
|---|---|
| Windows | File Explorer → **This PC** → **Map network drive** → enter the address. Windows only sends the password over HTTPS. |
| macOS | Finder → **Go** → **Connect to Server** → enter the address. |
| Linux (GNOME) | Files → **Other Locations** → connect to `davs://<your-domain>/dav/`. |
| Linux (KDE) | Dolphin → enter `webdavs://<your-domain>/dav/` in the location bar. |
| Linux (command line) | `rclone` with a WebDAV remote, or `davfs2` to mount it. |
| Android, iOS | Any file manager that supports WebDAV. |

If uploads of large files fail, the reverse proxy's request size limit is
probably too low; see
[Installation](installation.md#put-it-behind-the-reverse-proxy).

## The Untagged queue

**Untagged** lists the files in your folder, with a preview of each. The
number next to it in the navigation bar is how many are waiting.

For each file, enter its tags, tick **private** if it should be
[private](using.md#private-media), and click **Save to index**. The file is
indexed like an upload and removed from the folder. **Reject** deletes it
without indexing it.

Files that are already in the index are marked as duplicates. Files of a
type the index doesn't accept are marked as unsupported.

## How the inbox works

- Each user's folder is `staging/<user_id>/` on the server. Users can only
  see and change their own folder.
- A background scanner checks the folders every `STAGING_SCAN_INTERVAL`
  seconds. It takes a file once its size has stopped changing, or once it
  is older than `STAGING_STABLE_AGE` seconds, so a file still being copied
  isn't picked up half-written.
- The scanner computes each file's hash, so duplicates are known before you
  tag anything, and renders its preview. Saving to the index reuses that
  preview instead of rendering it again.
- Temporary and system files that clients leave behind (`.DS_Store`,
  `Thumbs.db`, `desktop.ini`, `*.part`, `*.tmp`, `*.crdownload`,
  `*.partial`) are ignored.
- Deleting or renaming a file through WebDAV updates the queue.

`DAV_ENABLED=false` turns WebDAV off. See [Configuration](configuration.md#web-app-web)
for the scanner settings.
