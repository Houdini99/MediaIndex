# HTTP endpoints

Every route the web app serves. Pages and their JSON helpers use the
browser's login session; the [upload API](#upload-api) and the [bot
endpoints](#bot-endpoints) authenticate with an app password instead.

Everything except the login pages, `/favicon.ico` and `/robots.txt` needs
a login, and every query only returns private items to their owner.

**Contents**

- [Authentication](#authentication)
- [Pages](#pages)
- [Media and tags (session)](#media-and-tags-session)
- [WebDAV inbox and settings (session)](#webdav-inbox-and-settings-session)
- [Upload API](#upload-api)
- [Bot endpoints](#bot-endpoints)
- [Admin](#admin)
- [WebDAV](#webdav)

## Authentication

- **Session:** log in through `/login` in a browser. Forms and `POST`
  requests made with the session need a CSRF token, as the pages provide.
- **App password:** HTTP Basic authentication with the username and the
  account's app password, generated on **Untagged** (`/queue`). Used by
  WebDAV, the upload API and the bot endpoints; it is never the login
  password. These requests need no CSRF token. A wrong app password counts
  as a failed login toward the [IP ban](security.md#logins-and-bans).

## Pages

| Route | Page |
|---|---|
| `GET /` | Gallery. Query: `search`, `exclude_tags`, `type` (`all`, `image`, `gif`, `video`), `sort` (`newest`, `oldest`, `random`), `hide_ai`, `private_only=1` |
| `GET /feed` | Feed |
| `GET /tags` | Tag list |
| `GET`, `POST /upload` | Upload page, and the upload form's target |
| `GET /queue` | **Untagged**: the WebDAV inbox queue and the app password |
| `GET /settings` | Settings: password, Telegram and Discord linking |
| `POST /settings/password` | Change your password: form fields `current_password`, `new_password` and `confirm_password`. 5 per minute. |
| `GET`, `POST /login`, `POST /logout` | Log in and out |
| `GET`, `POST /login/new-password` | Choose a password after an admin set one (a new account, or a reset on the admin panel), with the same fields. Until then, every other page redirects here. |
| `GET`, `POST /register` | Registration, closed unless `REGISTRATION_OPEN=true` |
| `GET /favicon.ico`, `GET /robots.txt` | No login needed |

## Media and tags (session)

| Route | Purpose |
|---|---|
| `GET /media/<file>`, `GET /thumbs/<file>` | A media file or thumbnail. Private files are decrypted on the fly, with `Range` support. |
| `GET /mediadata/<id>` | One item's details as JSON |
| `GET /api/media` | A gallery page as JSON, for infinite scrolling. Same query as `/`, plus `page` and `sort_seed` for random order. |
| `GET /api/feed` | A feed page as JSON. Query: `types`, `search`, `include_tags`, `exclude_tags`, `hide_ai`, `seed`, `page`. |
| `GET /api/tags` | Every tag the user can see, for suggestions |
| `POST /edit/<id>` | Set an item's tags (owner or admin) |
| `POST /delete/<id>` | Delete an item (owner or admin) |
| `POST /edit-bulk` | Add or remove tags on several items; JSON body with `ids` |
| `POST /delete-bulk` | Delete several items; JSON body with `ids` |
| `POST /privacy-bulk` | Make several items private or public; JSON body with `ids` |

The bulk routes apply the same ownership rule to each item.

## WebDAV inbox and settings (session)

| Route | Purpose |
|---|---|
| `GET /api/staging` | The user's files in the WebDAV inbox |
| `GET /staging/thumb/<id>`, `GET /staging/file/<id>` | A staged file's preview, or the file (owner only) |
| `POST /api/staging/<id>/promote` | Tag a staged file and add it to the index |
| `POST /api/staging/<id>/reject` | Delete a staged file without indexing it |
| `POST /api/dav/password` | Generate a new app password (shown once) |
| `POST /api/dav/password/revoke` | Remove the app password |
| `POST /api/telegram/link` | Link or unlink a Telegram account |
| `POST /api/discord/link/code` | Get a one-time Discord link code |
| `GET /api/discord/status` | The linked Discord ID, and whether a code is waiting |
| `POST /api/discord/unlink` | Unlink the Discord account |

## Upload API

`POST /api/upload` with the app password: any account can upload with it,
for example from a script. It is limited to 120 requests per minute.

Multipart form fields:

| Field | Meaning |
|---|---|
| `file` | A file; repeat for several (`files` works too) |
| `tags` | Tags for every file, comma-separated |
| `private` | `1`, `on` or `true` to store every file as private |
| `tags_<i>`, `private_<i>` | Override `tags` / `private` for the file at position `i` (from 0) |
| `uploaded_for` | Username to file the upload under. Only honoured for accounts in `DELEGATED_UPLOADERS`; others always upload as themselves. |

The answer lists one result per file:

```json
{
  "uploaded_by": "alice",
  "results": [
    {"filename": "cat.jpg", "status": "ok", "filehash": "<sha256>"},
    {"filename": "dog.jpg", "status": "duplicate", "filehash": "<sha256>",
     "duplicate_of": {"id": 42, "is_private": false, "filepath": "<sha256>.jpg",
                      "download_name": "<sha256>.jpg", "tags": "dog,park",
                      "uploaded_by": "bob"}},
    {"filename": "notes.txt", "status": "error", "filehash": null,
     "detail": "unsupported file type"}
  ]
}
```

When the duplicate is someone's private item, `duplicate_of` only contains
its `id` and `"is_private": true`. A private upload for a `uploaded_for`
user that doesn't exist is refused with an error, rather than filed under
the uploading account.

Example:

```bash
curl -u alice:<app-password> -F file=@cat.jpg -F tags=cats,funny \
  https://media.example.com/api/upload
```

## Bot endpoints

These use the app password and only answer accounts listed in
`DELEGATED_UPLOADERS`; any other account gets `403`.

| Route | Purpose |
|---|---|
| `GET /api/telegram/user?telegram_id=<id>&username=<name>` | Which index account a Telegram sender is linked to: `{"username": …, "pinned": …}`, or `404` if none |
| `GET /api/discord/user?discord_id=<id>` | Which index account a Discord user is linked to, or `404` if none |
| `POST /api/discord/verify` | Redeem a Discord link code: form fields `discord_id` and `code`. 30 per minute. |
| `GET /api/site-logins` | Which sites have a stored login (names and status only) |
| `GET /api/site-logins/cookies?url=<link>` | The `cookies.txt` to download that link with: its site's login, else *Other sites*. `404` when neither is stored. |
| `PUT /api/site-logins/<site>/cookies` | Store the cookies a download refreshed. `409` if the login was replaced or removed meanwhile, `422` if the new cookies would log the site out. |

Discord and Telegram IDs are sent as strings, since 19-digit numbers don't
survive a JavaScript number.

## Admin

Admin only, with the session.

| Route | Purpose |
|---|---|
| `GET /admin/` | Overview, users and IP bans |
| `POST /admin/users/create` | Create a user |
| `POST /admin/users/<id>/password` | Set a user's password; another user must choose their own at their next login |
| `POST /admin/users/<id>/admin` | Make or unmake an admin |
| `POST /admin/users/<id>/delete` | Delete a user, deleting or reassigning their media |
| `POST /admin/bans/ban`, `POST /admin/bans/lift` | Ban an address, or lift a ban |
| `POST /admin/tags/rename`, `POST /admin/tags/remove` | Rename or remove a tag everywhere, with an optional dry run |
| `GET /admin/integrity`, `POST /admin/integrity/cleanup` | The integrity report, and its fixes |
| `GET /admin/logs` | The log viewer |
| `GET /admin/site-logins` | Site logins |
| `POST /admin/site-logins/<site>/upload`, `…/remove` | Store or remove a site's login |
| `GET /admin/duplicates` | Duplicate finder |
| `POST /admin/duplicates/scan`, `…/cancel`, `GET …/status` | Start or cancel a fingerprint scan, and its progress |
| `GET /admin/duplicates/groups`, `GET …/export.csv` | Duplicate groups as JSON or CSV |
| `POST /admin/duplicates/resolve`, `…/resolve-all` | Move duplicates to the quarantine |
| `POST /admin/duplicates/dismiss`, `…/undismiss` | Mark a group as not duplicates, or undo that |
| `POST /admin/duplicates/quarantine/restore`, `…/purge` | Restore quarantined items, or delete them |

## WebDAV

`/dav/…` is a WebDAV server with HTTP Basic authentication by app
password. Each user only sees their own inbox folder. See [WebDAV
inbox](webdav.md).
