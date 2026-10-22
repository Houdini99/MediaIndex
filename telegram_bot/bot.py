"""Telegram → index ingest bot.

Everything a *linked* Telegram account sends — a photo, a video, or a
link to one — is indexed under that account's index user. Everything else
is dropped without being downloaded.

Two rules shape the whole module:

**Nothing untagged is indexed.** Media whose caption does not parse as a
tag list is parked (as a file_id, or a URL for a link — see pending.py)
and the sender is asked for tags. The tags arrive either as a reply to
that item or as a plain follow-up message, which tags everything the
sender has waiting.

**The gate comes first.** `_gate` runs before a file_id is parked, a
byte is downloaded, a URL is fetched, or a reply is composed, and it fails
closed — an unreachable web app pauses ingest rather than opening it.
Everything past that point reaches yt-dlp here and ffmpeg in the web
container, which runs as root.
"""
import asyncio
import html
import logging
import os
import tempfile
import uuid

import aiohttp
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from bots_common import resolver, site_logins
from bots_common.index_client import AuthError, IndexUnavailable

from . import config, download, parsing
from .index_client import IndexClient
from .pending import PendingStore

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
# httpx logs a line per API call at INFO, which at one getUpdates every few
# seconds buries everything this bot actually says.
logging.getLogger('httpx').setLevel(logging.WARNING)

BASE_TAGS = parsing.split_base_tags(config.INGEST_TAGS)


class _Item:
    """One ingestible thing, normalized away from Telegram's message shapes.

    A photo, a video, an animation and a document arrive as four different
    payloads with different fields; past this class they are all the same
    values. A link is the fifth shape: it carries a `url` instead of a
    `file_id`, and — when only yt-dlp can say what the file will turn out
    to be — an empty filename and extension that the resolver fills in
    before the upload.
    """
    __slots__ = ('file_id', 'filename', 'ext', 'size', 'message_id',
                 'caption', 'url', 'key', 'site')

    def __init__(self, file_id, filename, ext, size, message_id, caption,
                 url=None, key=None):
        self.file_id = file_id
        self.filename = filename
        self.ext = ext
        self.size = size
        self.message_id = message_id
        self.caption = caption
        self.url = url
        # The pending-store field this item parks under. Normally the
        # message it arrived on, so a reply can target it; links are keyed
        # per-URL because one message can carry several of them.
        self.key = key if key is not None else message_id
        self.site = None      # set by the resolver: 'youtube', 'tiktok', …

    def as_entry(self, prompt_message_id=None, private=False):
        return {
            'file_id': self.file_id,
            'filename': self.filename,
            'ext': self.ext,
            'size': self.size,
            'message_id': self.message_id,
            'prompt_message_id': prompt_message_id,
            'private': private,
            'url': self.url,
        }

    @classmethod
    def from_entry(cls, entry):
        # .get() on file_id, url and key: a link has no file_id, and an
        # entry parked by an older build has neither of the other two.
        return cls(entry.get('file_id'), entry['filename'], entry['ext'],
                   entry.get('size') or 0, entry['message_id'], None,
                   url=entry.get('url'), key=entry.get('key'))


def _label(name):
    """A filename or link, safe to drop into an HTML reply.

    Every reply goes out as HTML and everything named in one is chosen by
    the sender — a document called `a&b.mp4`, a link with a query string
    (`…?v=x&t=90`). Unescaped, Telegram rejects the whole message with
    "can't parse entities" and the sender hears nothing back at all.
    """
    return f"<code>{html.escape(name or '', quote=False)}</code>"


def extract_item(message):
    """The ingestible file in a message, or None.

    Telegram re-encodes photos and strips their filename, so a photo gets
    a synthetic one; `PhotoSize`s are ordered smallest-first, so the last
    is the full-resolution original.
    """
    caption = message.caption or ''
    if message.photo:
        photo = message.photo[-1]
        return _Item(photo.file_id, f"telegram_{message.message_id}.jpg",
                     'jpg', photo.file_size or 0, message.message_id, caption)
    for payload in (message.video, message.animation, message.document):
        if payload is None:
            continue
        name = getattr(payload, 'file_name', None)
        ext = parsing.file_ext(name)
        if not ext:
            # No filename (or no extension in it) — fall back to the MIME
            # subtype, which is how an animation or a camera video usually
            # arrives.
            mime = (getattr(payload, 'mime_type', '') or '').lower()
            ext = mime.rsplit('/', 1)[-1] if '/' in mime else ''
            ext = {'quicktime': 'mov', 'x-matroska': 'mkv',
                   'jpeg': 'jpg'}.get(ext, ext)
            name = f"telegram_{message.message_id}.{ext or 'bin'}"
        return _Item(payload.file_id, name, ext, payload.file_size or 0,
                     message.message_id, caption)
    return None


class IndexBot:
    def __init__(self, index=None, store=None, downloader=None):
        # `index`, `store` and `downloader` are injectable so tests can
        # drive the gate, the tag routing and the link paths against
        # doubles; production passes none of them.
        self.index = index or IndexClient(
            config.INDEX_INTERNAL_URL,
            config.INDEX_BOT_USERNAME,
            config.INDEX_BOT_APP_PASSWORD,
            auth_cooldown=config.AUTH_RETRY_SECONDS,
            lookup_ttl=config.LOOKUP_TTL)
        self.store = store or PendingStore()
        self.downloads = downloader or download.LinkDownloader()
        # media_group_id -> {'items': [...], 'task': Task, 'message': Message}
        self._albums = {}
        # Strong references: asyncio only weakly references scheduled
        # tasks, so without these the GC can destroy an album's flush
        # mid-await and the album is silently never ingested.
        self._album_tasks = set()

    # ── The ingest gate ────────────────────────────────────────────────
    async def _owner_for(self, user):
        """Index username that owns this sender's uploads, or None.

        The client caches the answer briefly, "not linked" included, so a
        flood from one unlinked account costs one lookup per minute rather
        than one per message. A rejected app password is an outage here.
        """
        try:
            return await self.index.lookup_telegram_user(user.id, user.username)
        except AuthError as e:
            logging.error("gate: index auth failed; backing off %.0fs",
                          self.index.blocked(), exc_info=True)
            raise IndexUnavailable("the index rejected the bot's login") from e

    async def _gate(self, message, user):
        """The owner for this sender, or None once the reason has been
        explained. Every handler starts here.

        Both refusals leave the sender without an ingest, but they are
        different situations and must read differently: "you are not
        linked" sends someone to the settings page, and saying that during
        an index restart would send a correctly-linked user to fix
        something that is not broken.
        """
        try:
            owner = await self._owner_for(user)
        except IndexUnavailable:
            await self._reply(
                message,
                "⏳ I can't reach the index right now, so I haven't saved "
                "anything. Send it again in a few minutes.")
            return None
        if owner is None:
            await self._refuse_unlinked(message, user)
            return None
        return owner

    async def _refuse_unlinked(self, message, user):
        """Tell an unlinked sender why nothing happened — at most once an
        hour, so a stranger cannot make the bot chatty."""
        logging.info("gate: dropping message from unlinked telegram id %s (@%s)",
                     user.id, user.username or '-')
        if not await self.store.claim_notice(user.id):
            return
        await self._reply(
            message,
            f"This Telegram account is not linked to an index user, so "
            f"nothing was saved.\n\nOpen {config.INDEX_PUBLIC_URL}/settings "
            f"and put this ID in the Telegram box:\n<code>{user.id}</code>")

    @staticmethod
    async def _reply(message, text):
        """Best-effort reply, returning the sent Message or None.

        A send failure must never kill a handler: a user who blocked the
        bot mid-album would otherwise abort the rest of the album.
        """
        try:
            return await message.reply_html(text,
                                            disable_web_page_preview=True)
        except TelegramError:
            logging.warning("could not reply to message %s", message.message_id,
                            exc_info=True)
            return None

    @staticmethod
    async def _delete(message):
        """Best-effort removal of one of the bot's own messages — the
        "downloading…" notice, which has said all it has to say once the
        result arrives. Never raises: a notice that outlives its download
        is untidy, not broken."""
        if message is None:
            return
        try:
            await message.delete()
        except TelegramError:
            logging.debug("could not delete a progress notice", exc_info=True)

    # ── Media ──────────────────────────────────────────────────────────
    async def on_media(self, update, context):
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        owner = await self._gate(message, user)
        if owner is None:
            return
        item = extract_item(message)
        if item is None:
            return
        if message.media_group_id:
            return self._buffer_album(message, item, context)
        await self._handle_batch(message, [item], item.caption, owner, context)

    def _buffer_album(self, message, item, context):
        """Collect one album, then handle it as a single batch.

        Telegram delivers an album as N separate updates sharing a
        media_group_id, and only ONE of them carries the caption — usually
        the first, but that is not guaranteed. Handling each update on its
        own would ingest one tagged photo and hold the other nine for tags
        that were already sent.

        Each arriving member restarts the timer, so the wait measures the
        gap between updates rather than capping the album's size.
        """
        gid = message.media_group_id
        album = self._albums.get(gid)
        if album is None:
            album = {'items': [], 'task': None, 'message': message,
                     'user': message.from_user}
            self._albums[gid] = album
        album['items'].append(item)
        if album['task'] is not None:
            album['task'].cancel()
        task = asyncio.create_task(self._flush_album(gid, context))
        album['task'] = task
        self._album_tasks.add(task)
        task.add_done_callback(self._album_task_done)

    def _album_task_done(self, task):
        self._album_tasks.discard(task)
        if not task.cancelled() and task.exception():
            logging.error("album: flush task crashed",
                          exc_info=task.exception())

    async def _flush_album(self, gid, context):
        await asyncio.sleep(config.ALBUM_WAIT)
        album = self._albums.pop(gid, None)
        if not album or not album['items']:
            return
        items = sorted(album['items'], key=lambda i: i.message_id)
        caption = parsing.group_caption([i.caption for i in items])
        owner = await self._gate(album['message'], album['user'])
        if owner is None:
            return
        await self._handle_batch(album['message'], items, caption, owner,
                                 context)

    async def _handle_batch(self, message, items, caption, owner, context):
        """Ingest a message's media if the caption names tags; otherwise
        park it and ask."""
        tags = parsing.parse_message_tags(caption)
        private = await self.store.get_flag(message.from_user.id, 'private')
        if tags:
            for item in items:
                await self._ingest(message, item, tags, owner, private, context)
            return
        prompt = await self._reply(
            message,
            f"🏷️ Send tags for {'these ' + str(len(items)) + ' files' if len(items) > 1 else 'this'} "
            f"and I'll index {'them' if len(items) > 1 else 'it'} "
            f"— e.g. <code>cats,funny</code>.\n"
            f"Reply to this message to tag only {'these' if len(items) > 1 else 'it'}, "
            f"or just send a tag list to tag everything you have waiting.")
        prompt_id = prompt.message_id if prompt else None
        for item in items:
            await self.store.add(message.from_user.id, item.key,
                                 item.as_entry(prompt_id, private))

    # ── Tags arriving as their own message ─────────────────────────────
    async def on_text(self, update, context):
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        owner = await self._gate(message, user)
        if owner is None:
            return
        # Links first: a message carrying one is never a tag list (a URL
        # disqualifies the whole string), so there is nothing to weigh up.
        urls = parsing.extract_urls(message.text) if config.LINKS_ENABLED else []
        if urls:
            await self._handle_links(message, urls, owner, context)
            return
        tags = parsing.parse_message_tags(message.text)
        if not tags:
            if await self.store.count(user.id):
                await self._reply(
                    message,
                    "That doesn't look like a tag list. Send something like "
                    "<code>cats,funny</code> — one line, comma-separated.")
            return
        # A reply targets exactly one item; a bare message drains
        # everything. Order matters: a reply is the more specific
        # intent, so it must be tested first.
        if message.reply_to_message:
            entry = await self._take_by_reply(
                user.id, message.reply_to_message.message_id)
            if entry:
                await self._ingest_entry(message, entry, tags, owner, context)
                return
            # Replying to something we aren't holding is not an error —
            # fall through and treat it as a plain tag message.
        entries = await self.store.drain(user.id)
        if not entries:
            await self._reply(message, "Nothing is waiting for tags.")
            return
        for entry in entries:
            await self._ingest_entry(message, entry, tags, owner, context)

    async def _take_by_reply(self, user_id, reply_message_id):
        """The waiting entry a reply refers to, removed from the store.

        A user may reply either to their own media message or to the
        bot's "send tags" prompt, and both are natural — so both are
        accepted. The direct hit covers the common case, where an item is
        keyed by the message it arrived on; the scan covers the two that
        are not — a reply to the prompt, and a link, which is keyed
        per-URL because one message can carry several.
        """
        entry = await self.store.take(user_id, reply_message_id)
        if entry:
            return entry
        for candidate in await self.store.list(user_id):
            if reply_message_id in (candidate.get('prompt_message_id'),
                                    candidate.get('message_id')):
                return await self.store.take(
                    user_id, candidate.get('key') or candidate['message_id'])
        return None

    async def _ingest_entry(self, message, entry, tags, owner, context):
        await self._ingest(message, _Item.from_entry(entry), tags, owner,
                           bool(entry.get('private')), context)

    # ── Links ──────────────────────────────────────────────────────────
    @staticmethod
    def _resolver_enabled():
        return config.LINKS_ENABLED and config.YTDLP_ENABLED \
            and resolver.available()

    async def _handle_links(self, message, urls, owner, context):
        """A message with links in it: index whatever can be downloaded
        from them, under whatever tags came alongside.

        The tag rules do not change for a link. `strip_urls` leaves the
        rest of the message as the tag candidate, so `cats,funny https://…`
        indexes straight away and a bare link is held and asked about —
        exactly as a photo is. Nothing is fetched until the tags exist:
        what is parked is the URL, never the bytes.
        """
        items = await self._link_items(message, urls)
        if not items:
            extra = ("" if self._resolver_enabled() else
                     "\n\n(The link resolver is unavailable on this "
                     "deployment, so only direct file links work.)")
            await self._reply(
                message,
                f"🚫 I can't download anything from that. Send a link to a "
                f"media file, or to a site yt-dlp supports (YouTube, TikTok, "
                f"Reddit, …).{extra}")
            return
        await self._handle_batch(message, items,
                                 parsing.strip_urls(message.text), owner,
                                 context)

    async def _link_items(self, message, urls):
        """The links in one message the bot can actually fetch, in the
        order they were sent.

        A URL that already names a media file is a plain download. Every
        other one is offered to yt-dlp's extractor list; the generic
        extractor is excluded, so only real platform pages go to yt-dlp.
        What is left gets one HEAD request, public addresses only, because
        plenty of CDNs serve a file from a URL that names none (a path
        like `…/ab/cd/<md5>` can be an mp4). When the server's
        Content-Type says media, it is a plain download too. Anything else
        is refused here rather than fetched blind.
        """
        urls = list(dict.fromkeys(urls))
        names = {url: parsing.media_url_info(url, config.ALLOWED_EXT)
                 for url in urls}
        pages = [url for url in urls if not names[url]]
        resolvable = set()
        if pages and self._resolver_enabled():
            # The first call compiles every extractor's URL regex (~2000
            # of them), which is far too slow for the event loop.
            resolvable = set(await asyncio.to_thread(
                resolver.filter_resolvable, pages))
        unclaimed = [url for url in pages if url not in resolvable]
        served = await self.downloads.sniff_links(
            unclaimed, config.ALLOWED_EXT) if unclaimed else {}
        items = []
        for n, url in enumerate(urls):
            fetch_url, name = served.get(url, (url, names[url]))
            if not name and url not in resolvable:
                logging.info("links: nothing ingestible behind %s", url)
                continue
            items.append(_Item(None, name or url, parsing.file_ext(name), 0,
                               message.message_id, None, url=fetch_url,
                               key=f"{message.message_id}#{n}"))
        return items

    async def _fetch_link(self, message, item, label):
        """Download what a link points at, or None once the failure has
        been reported.

        A URL that names a media file is streamed straight to disk;
        anything else goes to yt-dlp, which also decides what the file
        turns out to be — so the type check that a Telegram upload gets
        before its download happens here, after it.
        """
        notice = await self._reply(message, f"⬇️ Downloading {label} …")
        try:
            path = (await self._download_link(message, item, label)
                    if item.ext else
                    await self._resolve_link(message, item, label))
        finally:
            await self._delete(notice)
        if path is None:
            return None
        if item.ext not in config.ALLOWED_EXT:
            # yt-dlp muxes to mp4 whenever it merges two streams, but a
            # site that serves one pre-muxed format decides for itself.
            self._discard(path)
            await self._reply(
                message,
                f"🚫 {_label(item.filename)} came back as a "
                f".{item.ext or '?'} file, which the index does not take.")
            return None
        return path

    async def _download_link(self, message, item, label):
        """Stream a direct media link to a temp file."""
        cap = f"{config.LINK_MAX_BYTES / 1e6:.0f} MB"
        tmp = os.path.join(tempfile.gettempdir(),
                           f"tg-link-{uuid.uuid4().hex}.{item.ext}")
        try:
            await self.downloads.download(item.url, tmp, item.filename,
                                          config.LINK_MAX_BYTES)
        except download.Blocked as e:
            # Not a bad link so much as a refused one — say which, since
            # the sender may simply have pasted a LAN URL by accident.
            self._discard(tmp)
            await self._reply(
                message, f"🚫 {label} — {html.escape(str(e), quote=False)}.")
            return None
        except download.TooLarge as e:
            # Discard here rather than leaving it to the caller: an
            # aborted transfer has already written bytes, and returning
            # None means the caller never learns the path.
            self._discard(tmp)
            size = f" ({e.size / 1e6:.0f} MB)" if e.size else ""
            await self._reply(
                message, f"🚫 {label}{size} is over the {cap} link limit.")
            return None
        except aiohttp.ClientResponseError as e:
            logging.warning("links: %s returned HTTP %s", item.url, e.status)
            self._discard(tmp)
            await self._reply(
                message, f"❌ {label} — the site answered HTTP {e.status}.")
            return None
        except Exception as e:
            logging.warning("links: download of %s failed: %s",
                            item.url, type(e).__name__, exc_info=True)
            self._discard(tmp)
            await self._reply(
                message,
                f"❌ {label} — the download failed ({type(e).__name__}).")
            return None
        return tmp

    async def _resolve_link(self, message, item, label):
        """Hand a platform link to yt-dlp, logged in with the index's stored
        login for its site, and name the file it produced.

        The login is fetched first, and an index that can't be asked is
        reported as itself — resolving logged out instead would turn a
        restart into a "login required" verdict.
        """
        try:
            async with site_logins.cookies_for(
                    self.index, item.url, tempfile.gettempdir(),
                    config.YTDLP_COOKIES) as login:
                return await self._run_resolver(message, item, label, login)
        except AuthError:
            logging.error("resolver: index auth failed fetching the site "
                          "login; backing off %.0fs", self.index.blocked(),
                          exc_info=True)
            await self._reply(
                message, f"⏳ {label} — the index rejected my login. An "
                         f"admin needs to check the bot's app password.")
            return None
        except IndexUnavailable:
            await self._reply(
                message, f"⏳ {label} — I can't reach the index right now. "
                         f"Send it again in a few minutes.")
            return None

    async def _run_resolver(self, message, item, label, login):
        """The resolve itself. The blocking work goes to a thread under a
        hard ceiling that only covers a resolve hanging during
        *extraction*: once bytes are moving the resolver's own stall
        watchdog takes over, and that watches progress rather than total
        time, so a large but healthy download is never cut off.
        """
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(resolver.resolve, item.url,
                                  tempfile.gettempdir(),
                                  config.YTDLP_MAX_BYTES,
                                  login.path,
                                  config.YTDLP_STALL_TIMEOUT,
                                  quality=config.YTDLP_QUALITY,
                                  pot_provider=config.YTDLP_POT_PROVIDER),
                timeout=config.YTDLP_MAX_DURATION)
        except asyncio.TimeoutError:
            logging.warning("resolver: exceeded the %gs hard ceiling for %s",
                            config.YTDLP_MAX_DURATION, item.url)
            await self._reply(
                message,
                f"❌ {label} — it hung for {config.YTDLP_MAX_DURATION:g}s "
                f"without downloading anything.")
            return None
        except resolver.ResolveError as e:
            # Typed and specific (stalled, over the cap, blocked, no video
            # there, login required) — worth repeating verbatim instead of
            # "it failed", with what an admin can do about a login.
            reason = e.reason + login.hint(e)
            logging.warning("resolver: %s — %s", item.url, reason)
            await self._reply(
                message,
                f"{e.emoji} {label} — {html.escape(reason, quote=False)}.")
            return None
        except Exception as e:
            logging.exception("resolver: unexpected failure for %s", item.url)
            await self._reply(
                message,
                f"❌ {label} — the download failed ({type(e).__name__}).")
            return None
        if res is None:      # only reachable with the resolver disabled
            await self._reply(
                message, f"🚫 {label} — the link resolver is unavailable.")
            return None
        login.used = res.used_cookies
        item.filename = res.filename
        item.ext = parsing.file_ext(res.filename)
        item.site = res.site
        return res.path

    # ── Ingest ─────────────────────────────────────────────────────────
    async def _ingest(self, message, item, tags, owner, private, context):
        """Download one item and upload it to the index, then report."""
        label = _label(item.filename)
        # A link's type is only settled once it has been fetched — a
        # platform page becomes whatever yt-dlp muxes it into — so for
        # those this check happens on the far side of _fetch instead.
        if not item.url and item.ext not in config.ALLOWED_EXT:
            await self._reply(message, f"🚫 {label} — unsupported file type.")
            return
        if item.size and item.size > config.MAX_DOWNLOAD_BYTES:
            limit = parsing.human_size(config.MAX_DOWNLOAD_BYTES)
            extra = ("" if config.LOCAL_MODE else
                     " Telegram does not let bots download larger files — "
                     f"use the WebDAV queue at {config.INDEX_PUBLIC_URL}/queue.")
            await self._reply(
                message,
                f"🚫 {label} is {parsing.human_size(item.size)}, over the "
                f"{limit} limit.{extra}")
            return
        if self.index.blocked():
            await self._reply(
                message, f"⏳ {label} — the index rejected my login; I am "
                         f"backing off. Try again shortly.")
            return

        # The path is acquired and removed in one try/finally spanning both
        # the fetch and the upload: a download that fails *part way* has
        # already written bytes, so cleaning up only on the upload path
        # would leak a partial file per failure.
        path = None
        try:
            path = await self._fetch(message, item, label, context)
            if path is None:
                return
            # A resolved link only names itself now — yt-dlp reports the
            # video's title, and the site it came from rides along as a
            # tag the way it does on the Discord side.
            label = _label(item.filename)
            tag_str = parsing.merge_tags(
                list(tags) + ([item.site] if item.site else []), BASE_TAGS)
            try:
                result = await self.index.upload(path, item.filename, tag_str,
                                                 uploaded_for=owner,
                                                 private=private)
            except AuthError:
                logging.error("ingest: index auth failed; backing off %.0fs",
                              self.index.blocked(), exc_info=True)
                await self._reply(
                    message,
                    f"⏳ {label} — the index rejected my login. An admin "
                    f"needs to check the bot's app password.")
                return
            except Exception:
                logging.exception("ingest: upload of %s failed", item.filename)
                await self._reply(message, f"❌ {label} — the upload failed.")
                return
        finally:
            self._discard(path)
        await self._report(message, label, item, result, tag_str, private,
                           owner)

    async def _fetch(self, message, item, label, context):
        """Put the file on a path this process can read, or None once the
        failure has been reported.

        A link is fetched from the wider internet and has its own two
        paths (see `_fetch_link`). For a file the sender uploaded to
        Telegram there are two more, and the difference between *those* is
        the whole point of running a local Bot API server:

        * **Local server.** The file is already on disk in the shared work
          dir, and `download_to_drive()` *with no argument* hands back that
          path instead of copying it. Passing a custom path here would
          `shutil.copyfile` it — a second 2 GB on the volume for no reason.
        * **api.telegram.org.** PTB reads the whole response into memory
          before writing it out, which is survivable only because that path
          is capped at 20 MB.

        Either way the file is ours to delete afterwards: the local server
        never cleans its work dir up.
        """
        if item.url:
            return await self._fetch_link(message, item, label)
        try:
            tg_file = await context.bot.get_file(item.file_id)
        except TelegramError as e:
            logging.warning("ingest: could not fetch %s: %s", item.filename, e)
            await self._reply(
                message,
                f"❌ {label} — Telegram would not give me the file ({e}).")
            return None
        if not config.LOCAL_MODE:
            tmp = os.path.join(tempfile.gettempdir(),
                               f"tg-{item.file_id[:24]}.{item.ext}")
            try:
                await tg_file.download_to_drive(tmp)
            except TelegramError as e:
                logging.warning("ingest: download of %s failed: %s",
                                item.filename, e)
                # Discard here rather than leaving it to the caller: a
                # download that fails part way has already written bytes,
                # and returning None means the caller's cleanup never
                # learns the path.
                self._discard(tmp)
                await self._reply(
                    message, f"❌ {label} — the download failed ({e}).")
                return None
            return tmp
        # Local mode. PTB decides "is this a local file?" by stat-ing the
        # path the server reported (telegram._utils.files.is_local_file), so
        # a work dir that is not mounted here does not raise — it silently
        # falls back to treating the path as a URL and writing a junk file
        # into the working directory. Check it ourselves and say so.
        server_path = tg_file.file_path or ''
        if not os.path.isfile(server_path):
            logging.error(
                "ingest: the Bot API server reported %r, which this "
                "container cannot see — is the work dir volume mounted at "
                "the same path in both services?", server_path)
            await self._reply(
                message,
                f"❌ {label} — I can't reach the file the Bot API server "
                f"downloaded. An admin needs to check the shared work-dir "
                f"volume.")
            return None
        return str(await tg_file.download_to_drive())

    @staticmethod
    def _discard(path):
        """Remove a consumed file, whether it was our temp copy or the
        local server's original.

        A failure here is logged rather than raised: it never invalidates
        an ingest that already succeeded, and in local mode it is the
        symptom of one specific misconfiguration — the bot running as a uid
        that cannot write the server's work dir — which the message names
        so it does not have to be guessed at.
        """
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logging.warning(
                "ingest: could not delete %s — files will accumulate in the "
                "Bot API work dir. The bot container must run as the same "
                "uid as the telegram-bot-api service (101).", path,
                exc_info=True)

    async def _report(self, message, label, item, result, tag_str, private,
                      owner):
        # Tags and owner are echoed into an HTML reply; both can carry an
        # '&' (see _label for what that costs).
        shown = html.escape(parsing.format_tag_list(tag_str), quote=False)
        owner = html.escape(owner or '', quote=False)
        if result.status == 'ok':
            suffix = '.enc' if private else ''
            url = (f"{config.INDEX_PUBLIC_URL}/media/"
                   f"{result.filehash}.{item.ext}{suffix}")
            lock = '🔒 ' if private else ''
            await self._reply(
                message,
                f"✅ {lock}{label} indexed as <b>{owner}</b> — "
                f"tags: {shown}\n{url}")
            logging.info("ingest: %s -> %s (%s)", item.filename, owner, tag_str)
        elif result.status == 'duplicate':
            await self._reply(message,
                              f"♻️ {label} — {self._duplicate_note(result)}")
        else:
            detail = result.detail or 'the index refused it'
            await self._reply(message, f"❌ {label} — {detail}.")

    @staticmethod
    def _duplicate_note(result):
        """Describe what a duplicate collided with.

        A private item reports only that it exists: the index withholds
        its tags and owner on purpose, and repeating that omission here
        keeps the bot from becoming the side channel that leaks them.
        """
        dup = result.duplicate
        if not dup:
            return "already in the index"
        if dup.get('is_private'):
            return "already in the index (a private item)"
        tags = html.escape(parsing.format_tag_list(dup.get('tags')),
                           quote=False)
        who = html.escape(dup.get('uploaded_by') or 'someone', quote=False)
        note = f"already in the index, uploaded by {who}"
        return f"{note} — tags: {tags}" if tags else note

    # ── Commands ───────────────────────────────────────────────────────
    async def cmd_start(self, update, context):
        message, user = update.effective_message, update.effective_user
        # Not routed through _gate: /start is how someone finds out what
        # to paste into the website, so it must still answer usefully when
        # the index is unreachable.
        try:
            owner = await self._owner_for(user)
        except IndexUnavailable:
            owner = None
        if owner:
            await self._reply(
                message,
                f"👋 You are linked as <b>{owner}</b>.\n\n"
                f"Send me photos or videos with the tags in the caption "
                f"(<code>cats,funny</code>) — or a <b>link</b> to one, and "
                f"I'll download it (YouTube, TikTok, Reddit, a direct file "
                f"URL, …). No tags and I'll ask for them before saving "
                f"anything.\n\n"
                f"/private — save what you send next as private\n"
                f"/public — back to public\n"
                f"/pending — what is waiting for tags\n"
                f"/cancel — forget what is waiting")
            return
        await self._reply(
            message,
            f"👋 This Telegram account is not linked yet.\n\n"
            f"Open {config.INDEX_PUBLIC_URL}/settings, sign in, and paste "
            f"this ID into the Telegram box:\n\n<code>{user.id}</code>\n\n"
            f"Then send me media, or a link to some, and I'll index it "
            f"under your account.")

    async def cmd_private(self, update, context):
        await self._set_private(update, True)

    async def cmd_public(self, update, context):
        await self._set_private(update, False)

    async def _set_private(self, update, private):
        message, user = update.effective_message, update.effective_user
        if await self._gate(message, user) is None:
            return
        await self.store.set_flag(user.id, 'private', private)
        await self._reply(
            message,
            "🔒 What you send from now on will be saved as private "
            "(encrypted, visible only to you). /public undoes this."
            if private else
            "🌐 What you send from now on will be saved as public.")

    async def cmd_pending(self, update, context):
        message, user = update.effective_message, update.effective_user
        if await self._gate(message, user) is None:
            return
        entries = await self.store.list(user.id)
        if not entries:
            await self._reply(message, "Nothing is waiting for tags.")
            return
        names = '\n'.join(f"• {_label(e['filename'])}" for e in entries[:20])
        more = f"\n…and {len(entries) - 20} more" if len(entries) > 20 else ''
        await self._reply(
            message,
            f"{len(entries)} file(s) waiting for tags:\n{names}{more}\n\n"
            f"Send a tag list to index them all, or /cancel to forget them.")

    async def cmd_cancel(self, update, context):
        message, user = update.effective_message, update.effective_user
        if await self._gate(message, user) is None:
            return
        dropped = await self.store.clear(user.id)
        await self._reply(
            message,
            f"Forgot {dropped} waiting file(s)." if dropped
            else "Nothing was waiting.")


async def _on_error(update, context):
    logging.error("unhandled error while processing an update",
                  exc_info=context.error)


async def _post_shutdown(application):
    bot = application.bot_data.get('bot')
    if bot:
        await bot.index.close()
        await bot.store.close()
        await bot.downloads.close()


def build_application(bot=None):
    """Wire the handlers. Separated from main() so tests can inspect them."""
    bot = bot or IndexBot()
    builder = ApplicationBuilder().token(config.BOT_TOKEN)
    if config.API_BASE:
        # A local Bot API server lifts the 20 MB download ceiling to
        # 2000 MB, and in local mode getFile answers with a path on disk
        # instead of a URL — so the download becomes a file copy.
        builder = builder.base_url(f"{config.API_BASE}/bot").local_mode(True)
    application = builder.post_shutdown(_post_shutdown).build()
    application.bot_data['bot'] = bot

    application.add_handler(CommandHandler('start', bot.cmd_start))
    application.add_handler(CommandHandler('private', bot.cmd_private))
    application.add_handler(CommandHandler('public', bot.cmd_public))
    application.add_handler(CommandHandler('pending', bot.cmd_pending))
    application.add_handler(CommandHandler('cancel', bot.cmd_cancel))

    # Private chats only. In a group the bot would see every member's
    # media, and only the *sender's* link decides ownership — a group is
    # a confusing place for that, and Telegram's privacy mode would hide
    # most of it anyway.
    private_chat = filters.ChatType.PRIVATE
    media = (filters.PHOTO | filters.VIDEO | filters.ANIMATION
             | filters.Document.ALL)
    application.add_handler(MessageHandler(private_chat & media, bot.on_media))
    application.add_handler(MessageHandler(
        private_chat & filters.TEXT & ~filters.COMMAND, bot.on_text))
    application.add_error_handler(_on_error)
    return application


def _check_local_mode():
    """Warn loudly about a local-mode setup that will fail per-file.

    Both of these are silent at startup and only surface as a failed
    ingest much later, so they are worth naming here, at the one moment
    someone is actually reading the log.
    """
    work_dir = config.API_WORK_DIR
    if not os.path.isdir(work_dir):
        logging.error(
            "TELEGRAM_API_BASE is set but %s is not mounted in this "
            "container. getFile will answer with a path this process "
            "cannot see and every ingest will fail — mount the Bot API "
            "server's work dir at the same path in both services.",
            work_dir)
        return
    if not os.access(work_dir, os.W_OK):
        logging.warning(
            "%s is not writable by uid %s. Ingests will work, but consumed "
            "files cannot be deleted and the volume will grow without "
            "bound — run this container as the same uid as the "
            "telegram-bot-api service (101).", work_dir, os.getuid())


def _link_summary():
    """One phrase for the startup line: what this build will do with a
    link someone sends it."""
    if not config.LINKS_ENABLED:
        return "off"
    if not config.YTDLP_ENABLED:
        return "direct file links only (resolver disabled)"
    if not resolver.available():
        return "direct file links only (yt-dlp is not installed)"
    imp = "" if resolver.impersonation_available() else ", no impersonation"
    pot = (f", PO tokens via {config.YTDLP_POT_PROVIDER}"
           if config.YTDLP_POT_PROVIDER else ", no PO tokens")
    return (f"on (yt-dlp {resolver.version()}{imp}, "
            f"≤{config.YTDLP_QUALITY}p{pot}, site logins from the index)")


def main():
    gate = ("local Bot API server at " + config.API_BASE if config.LOCAL_MODE
            else "api.telegram.org (20 MB download limit)")
    logging.info("starting: index=%s as %s, base tags=%s, max file=%s, "
                 "via %s, links=%s",
                 config.INDEX_INTERNAL_URL, config.INDEX_BOT_USERNAME,
                 config.INGEST_TAGS,
                 parsing.human_size(config.MAX_DOWNLOAD_BYTES), gate,
                 _link_summary())
    if config.LINKS_ENABLED and config.YTDLP_ENABLED and not resolver.available():
        logging.warning("resolver: yt-dlp is not installed — links to a "
                        "platform page will be refused, and only links that "
                        "already name a media file will work")
    if config.LOCAL_MODE:
        _check_local_mode()
    # Long polling: no inbound port, no reverse-proxy route, the container
    # stays outbound-only — the same posture as the Discord gateway.
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
