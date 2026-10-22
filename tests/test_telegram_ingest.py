"""telegram_bot: the ingest gate, album batching, and the refuse-and-ask
tag flow.

Two rules carry the whole bot, and both are tested here as behaviour
rather than as implementation:

**Nothing untagged is indexed.** A caption that does not parse as a tag
list parks the item and asks; the tags then arrive as a reply (one item)
or as a plain message (everything waiting).

**The gate comes first.** An unlinked sender must not cause a download, a
parked item, or an upload — everything past that point reaches ffmpeg in
the web container, which runs as root.

python-telegram-bot and redis are faked (see conftest for the library
stub): these tests are about the bot's own logic, not about HTTP or about
Redis. Async is driven with asyncio.run, matching test_index_client.py.
"""
import asyncio
import tempfile

import pytest
from redis.exceptions import ResponseError
from telegram.error import TelegramError

from bots_common.index_client import IndexUnavailable
from telegram_bot import config
from telegram_bot.bot import IndexBot, extract_item
from telegram_bot.pending import PendingStore


def run(coro):
    return asyncio.run(coro)


# ── Doubles ───────────────────────────────────────────────────────────────
class FakeRedis:
    """In-memory stand-in for the handful of Redis commands PendingStore
    uses. Hash semantics only, plus SET NX EX for the notice throttle."""

    def __init__(self):
        self.hashes = {}
        self.strings = {}
        self.expires = {}

    # -- hash commands
    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        return sum(1 for f in fields if bucket.pop(f, None) is not None)

    async def hlen(self, key):
        return len(self.hashes.get(key, {}))

    async def expire(self, key, ttl):
        self.expires[key] = ttl
        return 1

    async def rename(self, key, newkey):
        if key not in self.hashes:
            raise ResponseError('no such key')
        self.hashes[newkey] = self.hashes.pop(key)
        return True

    async def delete(self, *keys):
        return sum(1 for k in keys if self.hashes.pop(k, None) is not None)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def aclose(self):
        pass

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._queued = []

    def __getattr__(self, name):
        def queue(*args, **kw):
            self._queued.append((name, args, kw))
            return self
        return queue

    async def execute(self):
        results = []
        for name, args, kw in self._queued:
            results.append(await getattr(self._redis, name)(*args, **kw))
        self._queued = []
        return results


class FakeIndex:
    """Stands in for IndexClient. Records what it was asked to do.

    `owner=None` is a sender nobody has linked; `raises` is the index
    being unreachable. The bot has to tell those two apart. `login` is the
    site login it hands out for every link (None: none stored), and
    `login_raises` the index failing to answer that question.
    """

    def __init__(self, owner='alice', status='ok', raises=None):
        self.owner = owner
        self.status = status
        self.raises = raises
        self.uploads = []
        self.lookups = []
        self.login = None
        self.login_raises = None
        self.login_requests = []
        self.stored_logins = []

    async def site_login(self, url):
        self.login_requests.append(url)
        if self.login_raises is not None:
            raise self.login_raises
        return self.login

    async def store_site_login(self, login, text):
        self.stored_logins.append((login.platform, login.revision, text))
        return login.revision + 1

    async def lookup_telegram_user(self, telegram_id, username=None):
        self.lookups.append((telegram_id, username))
        if self.raises is not None:
            raise self.raises
        return self.owner

    async def upload(self, path, filename, tags, uploaded_for=None,
                     private=False):
        self.uploads.append({'filename': filename, 'tags': tags,
                             'uploaded_for': uploaded_for, 'private': private})
        return _Result(self.status)


class _Result:
    def __init__(self, status):
        self.status = status
        self.detail = None
        self.filehash = 'a' * 64
        self.duplicate = None
        self.retryable = False


class FakeUser:
    def __init__(self, uid=42, username='alice_tg'):
        self.id = uid
        self.username = username


class FakeMessage:
    """Enough of a telegram.Message for the handlers under test."""
    _next_id = [1000]

    def __init__(self, user=None, caption='', text='', photo=None,
                 video=None, animation=None, document=None,
                 media_group_id=None, reply_to=None):
        FakeMessage._next_id[0] += 1
        self.message_id = FakeMessage._next_id[0]
        self.from_user = user or FakeUser()
        self.caption = caption
        self.text = text
        self.photo = photo or []
        self.video = video
        self.animation = animation
        self.document = document
        self.media_group_id = media_group_id
        self.reply_to_message = reply_to
        self.replies = []
        self.sent = []          # the Message objects reply_html handed back
        self.deleted = False

    async def reply_html(self, text, **kw):
        reply = FakeMessage(user=self.from_user, text=text)
        self.replies.append(text)
        self.sent.append(reply)
        return reply

    async def delete(self):
        self.deleted = True


class FakePhoto:
    def __init__(self, file_id='photo-1', size=1000):
        self.file_id = file_id
        self.file_size = size


class FakeDocument:
    def __init__(self, file_id='doc-1', name='clip.mp4', size=1000,
                 mime='video/mp4'):
        self.file_id = file_id
        self.file_name = name
        self.file_size = size
        self.mime_type = mime


class FakeUpdate:
    def __init__(self, message):
        self.effective_message = message
        self.effective_user = message.from_user


class FakeContext:
    """context.bot.get_file(...) → a file that models both transports.

    `server_path` switches it into local-Bot-API-server behaviour: the
    file already exists on a shared volume, and download_to_drive() called
    with no argument returns that path instead of copying it (which is
    what real PTB does — telegram.File.download_to_drive).
    """

    def __init__(self, tmp_path, server_path=None):
        self.bot = self
        self.tmp_path = tmp_path
        self.server_path = server_path
        self.fetched = []

    async def get_file(self, file_id):
        self.fetched.append(file_id)
        return _FakeFile(file_id, self.server_path)


class _FakeFile:
    def __init__(self, file_id, server_path=None):
        self.file_id = file_id
        self.file_path = str(server_path) if server_path else None
        self.copied_to = None

    async def download_to_drive(self, path=None):
        if path is None:
            # Local mode: PTB hands back the server's own path, no copy.
            return self.file_path
        self.copied_to = path
        with open(path, 'wb') as fh:
            fh.write(b'not really media')
        return path


@pytest.fixture()
def store():
    return PendingStore(client=FakeRedis(), ttl=60, limit=5)


@pytest.fixture()
def bot(store):
    return IndexBot(index=FakeIndex(), store=store)


@pytest.fixture()
def ctx(tmp_path):
    return FakeContext(tmp_path)


def photo_message(caption='', **kw):
    return FakeMessage(photo=[FakePhoto('small', 10), FakePhoto('full', 1000)],
                       caption=caption, **kw)


# ── extract_item ──────────────────────────────────────────────────────────
def test_photo_takes_the_largest_size():
    """PhotoSizes are ordered smallest-first; the last is the original."""
    item = extract_item(photo_message())
    assert item.file_id == 'full'
    assert item.ext == 'jpg'


def test_document_keeps_its_filename():
    item = extract_item(FakeMessage(document=FakeDocument(name='cat.webm')))
    assert item.filename == 'cat.webm' and item.ext == 'webm'


def test_extension_falls_back_to_the_mime_type():
    """A camera video often arrives with no usable filename."""
    item = extract_item(FakeMessage(
        document=FakeDocument(name=None, mime='video/quicktime')))
    assert item.ext == 'mov'


def test_a_message_with_no_media_yields_nothing():
    assert extract_item(FakeMessage(text='hello')) is None


# ── The gate ──────────────────────────────────────────────────────────────
def test_unlinked_sender_is_not_downloaded_parked_or_uploaded(bot, ctx, store):
    bot.index.owner = None
    msg = photo_message(caption='cats,funny')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert ctx.fetched == [], "the gate must run before any download"
    assert bot.index.uploads == []
    assert run(store.count(msg.from_user.id)) == 0
    assert len(msg.replies) == 1, "the sender is told once why nothing happened"


def test_the_unlinked_notice_is_throttled(bot, ctx):
    bot.index.owner = None
    user = FakeUser()
    first = photo_message(user=user)
    second = photo_message(user=user)
    run(bot.on_media(FakeUpdate(first), ctx))
    run(bot.on_media(FakeUpdate(second), ctx))
    assert len(first.replies) == 1
    assert second.replies == [], "a stranger must not make the bot chatty"


# ── Captioned media ingests straight away ─────────────────────────────────
def test_caption_tags_are_merged_with_the_base_tags(bot, ctx):
    run(bot.on_media(FakeUpdate(photo_message(caption='cats,funny')), ctx))
    assert len(bot.index.uploads) == 1
    upload = bot.index.uploads[0]
    assert upload['tags'] == 'cats,funny,telegram'
    assert upload['uploaded_for'] == 'alice'
    assert upload['private'] is False


def test_prose_does_not_count_as_tags(bot, ctx, store):
    msg = photo_message(caption='look at this lol')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert bot.index.uploads == []
    assert run(store.count(msg.from_user.id)) == 1
    assert '🏷️' in msg.replies[0]


def test_unsupported_type_is_refused_without_downloading(bot, ctx):
    msg = FakeMessage(document=FakeDocument(name='notes.pdf',
                                            mime='application/pdf'),
                      caption='docs')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert ctx.fetched == [] and bot.index.uploads == []
    assert 'unsupported' in msg.replies[0]


def test_oversized_file_is_refused_before_the_download(bot, ctx, monkeypatch):
    monkeypatch.setattr(config, 'MAX_DOWNLOAD_BYTES', 1024)
    msg = FakeMessage(document=FakeDocument(name='big.mp4', size=99 * 1024),
                      caption='big')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert ctx.fetched == [] and bot.index.uploads == []
    assert 'limit' in msg.replies[0]


# ── The refuse-and-ask flow ───────────────────────────────────────────────
def test_a_follow_up_tag_message_drains_everything_waiting(bot, ctx, store):
    user = FakeUser()
    first, second = photo_message(user=user), photo_message(user=user)
    run(bot.on_media(FakeUpdate(first), ctx))
    run(bot.on_media(FakeUpdate(second), ctx))
    assert run(store.count(user.id)) == 2

    tag_msg = FakeMessage(user=user, text='holiday,2026')
    run(bot.on_text(FakeUpdate(tag_msg), ctx))
    assert len(bot.index.uploads) == 2
    assert all(u['tags'] == 'holiday,2026,telegram' for u in bot.index.uploads)
    assert run(store.count(user.id)) == 0


def test_a_reply_tags_only_the_item_it_answers(bot, ctx, store):
    """Without this, a user holding two untagged items has no way to give
    them different tags."""
    user = FakeUser()
    first, second = photo_message(user=user), photo_message(user=user)
    run(bot.on_media(FakeUpdate(first), ctx))
    run(bot.on_media(FakeUpdate(second), ctx))

    reply = FakeMessage(user=user, text='just-this', reply_to=first)
    run(bot.on_text(FakeUpdate(reply), ctx))
    assert len(bot.index.uploads) == 1
    assert bot.index.uploads[0]['tags'] == 'just-this,telegram'
    assert run(store.count(user.id)) == 1, "the other item still waits"


def test_a_reply_to_the_bots_prompt_also_works(bot, ctx, store):
    """Replying to the 'send tags' prompt is at least as natural as
    replying to one's own photo, so both are accepted."""
    user = FakeUser()
    msg = photo_message(user=user)
    run(bot.on_media(FakeUpdate(msg), ctx))
    entry = run(store.list(user.id))[0]
    prompt = FakeMessage(user=user)
    prompt.message_id = entry['prompt_message_id']

    reply = FakeMessage(user=user, text='tagged', reply_to=prompt)
    run(bot.on_text(FakeUpdate(reply), ctx))
    assert len(bot.index.uploads) == 1
    assert run(store.count(user.id)) == 0


def test_tags_for_nothing_waiting_say_so(bot, ctx):
    msg = FakeMessage(text='cats,funny')
    run(bot.on_text(FakeUpdate(msg), ctx))
    assert bot.index.uploads == []
    assert 'Nothing is waiting' in msg.replies[0]


def test_prose_while_items_wait_is_explained(bot, ctx):
    user = FakeUser()
    run(bot.on_media(FakeUpdate(photo_message(user=user)), ctx))
    msg = FakeMessage(user=user, text='so what do I do now')
    run(bot.on_text(FakeUpdate(msg), ctx))
    assert bot.index.uploads == []
    assert "doesn't look like a tag list" in msg.replies[0]


def test_the_private_flag_survives_into_the_upload(bot, ctx, store):
    user = FakeUser()
    run(store.set_flag(user.id, 'private', True))
    run(bot.on_media(FakeUpdate(photo_message(user=user, caption='secret')), ctx))
    assert bot.index.uploads[0]['private'] is True


# ── Albums ────────────────────────────────────────────────────────────────
def _run_album(bot, ctx, captions, monkeypatch):
    """Deliver one album as Telegram does: N updates, one media_group_id,
    the caption on exactly one of them."""
    monkeypatch.setattr(config, 'ALBUM_WAIT', 0.01)
    user = FakeUser()

    async def deliver():
        messages = [photo_message(user=user, caption=c, media_group_id='g1')
                    for c in captions]
        for msg in messages:
            await bot.on_media(FakeUpdate(msg), ctx)
        # Let the debounce fire and the flush finish.
        await asyncio.sleep(0.05)
        while bot._album_tasks:
            await asyncio.gather(*list(bot._album_tasks),
                                 return_exceptions=True)
        return messages

    return run(deliver()), user


def test_an_albums_single_caption_tags_every_member(bot, ctx, monkeypatch):
    """The bug this prevents: Telegram puts the caption on one item of a
    media group, so per-update handling would ingest one photo and hold
    the rest for tags that were already sent."""
    _run_album(bot, ctx, ['', 'cats,funny', ''], monkeypatch)
    assert len(bot.index.uploads) == 3
    assert all(u['tags'] == 'cats,funny,telegram' for u in bot.index.uploads)


def test_the_caption_is_found_wherever_it_sits(bot, ctx, monkeypatch):
    _run_album(bot, ctx, ['', '', 'trailing'], monkeypatch)
    assert len(bot.index.uploads) == 3
    assert all(u['tags'] == 'trailing,telegram' for u in bot.index.uploads)


def test_an_uncaptioned_album_parks_every_item_but_asks_once(bot, ctx, store,
                                                             monkeypatch):
    messages, user = _run_album(bot, ctx, ['', '', ''], monkeypatch)
    assert bot.index.uploads == []
    assert run(store.count(user.id)) == 3
    prompts = [r for m in messages for r in m.replies if '🏷️' in r]
    assert len(prompts) == 1, "one album, one prompt"


def test_tagging_an_uncaptioned_album_ingests_all_of_it(bot, ctx, store,
                                                        monkeypatch):
    _messages, user = _run_album(bot, ctx, ['', '', ''], monkeypatch)
    run(bot.on_text(FakeUpdate(FakeMessage(user=user, text='holiday')), ctx))
    assert len(bot.index.uploads) == 3
    assert run(store.count(user.id)) == 0


def test_an_unreachable_index_is_not_reported_as_unlinked(bot, ctx, store):
    """Telling a correctly-linked user they are not linked would send them
    to the settings page to fix something that is not broken."""
    bot.index.raises = IndexUnavailable('down')
    msg = photo_message(caption='cats,funny')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert ctx.fetched == [] and bot.index.uploads == []
    assert run(store.count(msg.from_user.id)) == 0
    assert "can't reach the index" in msg.replies[0]
    assert 'not linked' not in msg.replies[0]


def test_an_outage_is_not_cached_as_a_verdict(bot, ctx):
    """Caching the failure would keep the gate shut for the rest of the
    TTL after the index came back."""
    bot.index.raises = IndexUnavailable('down')
    run(bot.on_media(FakeUpdate(photo_message(caption='a')), ctx))
    bot.index.raises = None
    run(bot.on_media(FakeUpdate(photo_message(caption='cats')), ctx))
    assert len(bot.index.uploads) == 1


def test_start_still_helps_while_the_index_is_down(bot, ctx):
    """/start is how someone learns what to paste into the website, so it
    must answer usefully even when the lookup cannot run."""
    bot.index.raises = IndexUnavailable('down')
    msg = FakeMessage(text='/start')
    run(bot.cmd_start(FakeUpdate(msg), ctx))
    assert str(msg.from_user.id) in msg.replies[0]


# ── Local Bot API server (the 20 MB workaround) ───────────────────────────
def _local(monkeypatch, work_dir):
    monkeypatch.setattr(config, 'LOCAL_MODE', True)
    monkeypatch.setattr(config, 'API_WORK_DIR', str(work_dir))
    monkeypatch.setattr(config, 'MAX_DOWNLOAD_BYTES', 2000 * 1024 * 1024)


def test_local_mode_uploads_the_servers_file_without_copying_it(
        bot, tmp_path, monkeypatch):
    """The point of the whole exercise: at 2 GB a shutil.copyfile would
    put a second copy on the volume for no reason."""
    _local(monkeypatch, tmp_path)
    served = tmp_path / 'file_1.jpg'
    served.write_bytes(b'real bytes')
    ctx = FakeContext(tmp_path, server_path=served)

    run(bot.on_media(FakeUpdate(photo_message(caption='cats')), ctx))
    assert len(bot.index.uploads) == 1
    assert bot.index.uploads[0]['tags'] == 'cats,telegram'


def test_local_mode_deletes_the_file_it_consumed(bot, tmp_path, monkeypatch):
    """The local server never cleans its work dir up, so the bot must —
    otherwise the volume grows by up to 2000 MB per ingest."""
    _local(monkeypatch, tmp_path)
    served = tmp_path / 'file_2.jpg'
    served.write_bytes(b'real bytes')
    ctx = FakeContext(tmp_path, server_path=served)

    run(bot.on_media(FakeUpdate(photo_message(caption='cats')), ctx))
    assert not served.exists()


def test_local_mode_refuses_when_the_work_dir_is_not_shared(
        bot, tmp_path, monkeypatch):
    """PTB decides "is this local?" by stat-ing the path, so an unmounted
    work dir does not raise — it silently writes a junk file. The bot has
    to catch that itself and say so."""
    _local(monkeypatch, tmp_path)
    ctx = FakeContext(tmp_path, server_path=tmp_path / 'never-written.jpg')

    msg = photo_message(caption='cats')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert bot.index.uploads == []
    assert 'shared work-dir' in msg.replies[0]


def test_local_mode_raises_the_size_ceiling(bot, tmp_path, monkeypatch):
    """The same 100 MB file that api.telegram.org refuses goes through."""
    _local(monkeypatch, tmp_path)
    served = tmp_path / 'big.mp4'
    served.write_bytes(b'x')
    ctx = FakeContext(tmp_path, server_path=served)

    msg = FakeMessage(document=FakeDocument(name='big.mp4',
                                            size=100 * 1024 * 1024),
                      caption='big')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert len(bot.index.uploads) == 1


def test_cloud_mode_still_copies_to_a_temp_file(bot, ctx):
    """The non-local path is unchanged: PTB is handed an explicit path."""
    run(bot.on_media(FakeUpdate(photo_message(caption='cats')), ctx))
    assert len(bot.index.uploads) == 1
    assert ctx.fetched == ['full']


def test_an_undeletable_file_does_not_fail_the_ingest(bot, tmp_path,
                                                      monkeypatch, caplog):
    """A cleanup failure is a misconfiguration to report, never a reason to
    lose an upload that already succeeded."""
    _local(monkeypatch, tmp_path)
    served = tmp_path / 'file_3.jpg'
    served.write_bytes(b'real bytes')
    ctx = FakeContext(tmp_path, server_path=served)

    def boom(path):
        raise PermissionError(13, 'Permission denied')
    monkeypatch.setattr('telegram_bot.bot.os.remove', boom)

    run(bot.on_media(FakeUpdate(photo_message(caption='cats')), ctx))
    assert len(bot.index.uploads) == 1
    assert 'accumulate' in caplog.text


def test_a_failed_download_leaves_no_temp_file(bot, ctx, monkeypatch,
                                               tmp_path):
    """A download that fails part way has already written bytes."""
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path))

    async def half_written(path=None):
        with open(path, 'wb') as fh:
            fh.write(b'partial')
        raise TelegramError('connection reset')

    original = ctx.get_file

    async def get_file(file_id):
        f = await original(file_id)
        f.download_to_drive = half_written
        return f
    ctx.get_file = get_file

    msg = photo_message(caption='cats')
    run(bot.on_media(FakeUpdate(msg), ctx))
    assert bot.index.uploads == []
    assert list(tmp_path.iterdir()) == []
