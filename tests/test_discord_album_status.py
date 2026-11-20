"""discord_bot/album_status.py: one status message per Bunkr album.

The complaint this pins: a 20-file album posted twenty "✅ … indexed
privately for **alice** — tags: …" lines. Its files now report into
one message, edited as each settles, with who and which tags said once at
the top; a new message starts only when Discord's 2000-character limit
would be crossed.

The first half drives AlbumStatus with a fake post/edit pair, the second
the bot's _ingest_all, so the routing (album files into the album's
message, everything else on its own line) is pinned too.
"""
import asyncio
import types

import pytest

from bots_common import bunkr
from bots_common.download import TooLarge
from discord_bot import bot as botmod
from discord_bot import config
from discord_bot.album_status import MAX_LINE, AlbumStatus
from test_discord_dm_ingest import FakeIndex, FakeQueue

discord = botmod.discord
ALBUM_URL = 'https://bunkr.cr/a/Qw3rTy12'


def run(coro):
    return asyncio.run(coro)


def album(n=3, title='Holiday 2022', **kw):
    files = [(f'https://bunkr.cr/f/img{i}-Q.jpg', f'img{i}.jpg')
             for i in range(n)]
    return bunkr.Album(ALBUM_URL, title, files, n, **kw)


class Sent:
    """A posted Discord message: its text, and every edit made to it."""

    def __init__(self, text):
        self.text = text
        self.edits = 0


class Board:
    """Fake post/edit for an AlbumStatus: records every message."""

    def __init__(self, fail_edits=False, post_fails=0):
        self.messages = []
        self.fail_edits = fail_edits
        self.post_fails = post_fails

    async def post(self, text):
        if self.post_fails:
            self.post_fails -= 1
            return None
        self.messages.append(Sent(text))
        return self.messages[-1]

    async def edit(self, sent, text):
        if self.fail_edits:
            return False
        sent.text = text
        sent.edits += 1
        return True

    def texts(self):
        return [m.text for m in self.messages]


def status(board, alb=None, room=2000, interval=0.0, **kw):
    return AlbumStatus(alb or album(), board.post, board.edit, room,
                       interval=interval, **kw)


# ── The message ───────────────────────────────────────────────────────────
def test_an_albums_files_are_one_message_with_who_and_tags_said_once():
    board = Board()

    async def go():
        s = status(board)
        for i in range(3):
            s.indexed_as("Indexed privately for **alice** — tags: `x`")
            await s.add(f"✅ `img{i}.jpg`")
        await s.finish()
    run(go())

    assert board.texts() == ['📚 <https://bunkr.cr/a/Qw3rTy12> “Holiday 2022”\n'
                             'Indexed privately for **alice** — tags: `x`\n'
                             '✅ `img0.jpg`\n✅ `img1.jpg`\n✅ `img2.jpg`']


def test_progress_shows_while_the_album_runs_and_goes_when_it_is_done():
    board = Board()

    async def go():
        s = status(board)
        await s.add("✅ `img0.jpg`")
        running = board.messages[0].text
        await s.finish()
        return running
    running = run(go())

    assert running.endswith('✅ `img0.jpg`\n⬇️ 1/3…')
    assert '⬇️' not in board.messages[0].text


def test_what_the_album_left_out_is_said_at_the_top():
    board = Board()

    async def go():
        s = status(board, album(2, over_limit=40))
        await s.add("✅ `img0.jpg`")
        await s.finish()
    run(go())

    assert board.texts()[0].splitlines()[1] == (
        'Taking the first 2 of the 42 images and videos in “Holiday 2022” '
        '(the album limit)')


def test_the_first_summary_stays():
    board = Board()

    async def go():
        s = status(board)
        s.indexed_as('Indexed for **alice** — tags: `a`')
        s.indexed_as('Indexed for **bob** — tags: `b`')
        await s.add("✅ `img0.jpg`")
        await s.finish()
    run(go())
    assert 'alice' in board.texts()[0] and 'bob' not in board.texts()[0]


def test_an_album_without_a_title_still_names_its_link():
    board = Board()

    async def go():
        s = status(board, album(1, title=''))
        await s.add("✅ `img0.jpg`")
        await s.finish()
    run(go())
    assert board.texts()[0].startswith('📚 <https://bunkr.cr/a/Qw3rTy12>\n')


def test_nothing_settled_posts_nothing():
    board = Board()
    run(status(board).finish())
    assert board.messages == []


# ── Too long for one message ──────────────────────────────────────────────
def test_a_message_that_would_overflow_carries_on_in_a_new_one():
    board = Board()
    lines = [f"✅ `{'x' * 40}-{i}.jpg`" for i in range(12)]

    async def go():
        s = status(board, album(12), room=300)
        for line in lines:
            await s.add(line)
        await s.finish()
    run(go())

    assert len(board.messages) > 1
    assert all(len(text) <= 300 for text in board.texts())
    assert board.texts()[1].startswith(
        '📚 <https://bunkr.cr/a/Qw3rTy12> “Holiday 2022” (continued)\n')
    shown = [line for text in board.texts() for line in text.splitlines()
             if line.startswith('✅')]
    assert shown == lines, "every line, once, in order"
    assert not any('⬇️' in text for text in board.texts()), \
        "a full message keeps no progress line"


def test_one_overlong_line_is_cut_rather_than_filling_a_message():
    board = Board()

    async def go():
        s = status(board)
        await s.add('♻️ ' + 'y' * 5000)
        await s.finish()
    run(go())
    [line] = [x for x in board.texts()[0].splitlines() if x.startswith('♻️')]
    assert len(line) == MAX_LINE and line.endswith('…')


# ── Editing ───────────────────────────────────────────────────────────────
def test_edits_inside_the_window_wait_and_finish_writes_the_rest():
    board = Board()
    now = [0.0]

    async def go():
        s = status(board, interval=60, clock=lambda: now[0])
        await s.add("✅ `img0.jpg`")              # posted straight away
        await s.add("✅ `img1.jpg`")              # inside the window
        await s.add("✅ `img2.jpg`")
        edits_before_finish = board.messages[0].edits
        await s.finish()
        return edits_before_finish
    edits_before_finish = run(go())

    assert len(board.messages) == 1
    assert edits_before_finish == 0
    assert board.messages[0].edits == 1
    assert board.texts()[0].endswith('✅ `img2.jpg`')


def test_a_line_inside_the_window_still_shows_without_waiting_for_the_next():
    """The next file may be a video that takes minutes; the line before it
    must not wait for that."""
    board = Board()

    async def go():
        s = status(board, interval=0.05)
        await s.add("✅ `img0.jpg`")
        await s.add("✅ `img1.jpg`")
        await asyncio.sleep(0.2)                 # no further line arrives
        shown = board.messages[0].text
        await s.finish()
        return shown
    shown = run(go())
    assert '✅ `img1.jpg`' in shown


def test_a_message_that_cannot_be_edited_is_posted_again():
    board = Board(fail_edits=True)

    async def go():
        s = status(board)
        await s.add("✅ `img0.jpg`")
        await s.add("✅ `img1.jpg`")
        await s.finish()
    run(go())
    assert board.texts()[-1].endswith('✅ `img0.jpg`\n✅ `img1.jpg`')


def test_nowhere_to_post_is_not_an_error():
    """No bot-logs channel configured: _report posts nothing and returns
    None, and the album carries on regardless."""
    board = Board(post_fails=10)

    async def go():
        s = status(board)
        await s.add("✅ `img0.jpg`")
        await s.finish()
    run(go())
    assert board.messages == []


# ── Through the bot ───────────────────────────────────────────────────────
class FakeSent(Sent):
    """A discord.Message as far as the bot uses one: it can be edited."""

    async def edit(self, content, **kw):
        self.text = content
        self.edits += 1


class LogChannel:
    def __init__(self):
        self.messages = []

    async def send(self, text, **kw):
        self.messages.append(FakeSent(text))
        return self.messages[-1]


class FakeDM(discord.DMChannel):
    def __init__(self):
        self.id = 700
        self.messages = []

    async def send(self, content, **kw):
        self.messages.append(FakeSent(content))
        return self.messages[-1]


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(config, 'LOG_CHANNEL_ID', 999)
    monkeypatch.setattr(config, 'INGEST_TAGS', 'discord')
    b = botmod.IndexBot.__new__(botmod.IndexBot)
    b.index = FakeIndex(links={42: 'alice'})
    b._retry = FakeQueue()
    b.log_channel = LogChannel()
    b._log_channel = b.log_channel
    b.downloads, b.reactions, b.fail = [], [], {}

    async def download(src, dest):
        b.downloads.append(src.url)
        if src.url in b.fail:
            raise b.fail[src.url]

    async def react(message, emoji):
        b.reactions.append(emoji)

    async def unreact(message, emoji):
        pass

    b._downloader = types.SimpleNamespace(download=download)
    b._react, b._unreact = react, unreact
    return b


def _channel_message():
    return types.SimpleNamespace(
        id=600, content='', channel=types.SimpleNamespace(id=1),
        author=types.SimpleNamespace(id=42, display_name='alice'),
        jump_url='https://discord.test/channel', message_snapshots=[])


def _dm_message():
    return types.SimpleNamespace(
        id=500, content='', channel=FakeDM(),
        author=types.SimpleNamespace(id=42, display_name='alice'),
        jump_url='https://discord.test/dm', message_snapshots=[],
        to_reference=lambda **kw: 500)


def _sources(alb):
    return [botmod._UrlSource(link, name, alb) for link, name in alb.files]


def test_a_channel_album_is_one_bot_logs_message(bot):
    alb = album(3)
    run(bot._ingest_all(_channel_message(), _sources(alb), ['beach'], 0))

    [msg] = bot.log_channel.messages
    assert msg.text == ('📚 <https://bunkr.cr/a/Qw3rTy12> “Holiday 2022”\n'
                        'Indexed for **alice** — tags: `beach,discord`\n'
                        '✅ `img0.jpg`\n✅ `img1.jpg`\n✅ `img2.jpg`\n'
                        'https://discord.test/channel')
    assert len(bot.index.uploads) == 3


def test_a_dm_album_is_one_reply_in_the_dm(bot):
    message = _dm_message()
    run(bot._ingest_all(message, _sources(album(2)), [], 0))

    [msg] = message.channel.messages
    assert msg.text.splitlines()[1] == \
        'Indexed privately for **alice** — tags: `discord`'
    assert msg.text.endswith('✅ `img1.jpg`')
    assert bot.log_channel.messages == []


def test_a_failed_file_is_a_line_in_the_albums_message(bot, monkeypatch):
    monkeypatch.setattr(config, 'LINK_MAX_BYTES', 512_000_000)
    alb = album(3)
    bot.fail[alb.files[1][0]] = TooLarge(900_000_000)

    run(bot._ingest_all(_channel_message(), _sources(alb), [], 0))

    [msg] = bot.log_channel.messages
    assert '🚫 `img1.jpg` (900 MB) is over the 512 MB link limit' in msg.text
    assert msg.text.count('✅') == 2


def test_other_sources_keep_their_own_line(bot):
    alb = album(2)
    loose = botmod._UrlSource('https://cdn.test/cat.png', 'cat.png')

    run(bot._ingest_all(_channel_message(), [loose, *_sources(alb)], [], 0))

    texts = [m.text for m in bot.log_channel.messages]
    assert len(texts) == 2
    assert texts[0].startswith('✅ `cat.png` indexed for **alice**')
    assert texts[1].startswith('📚 <https://bunkr.cr/a/Qw3rTy12>')


def test_two_albums_are_two_messages(bot):
    first = album(2)
    second = bunkr.Album('https://bunkr.cr/a/other', 'Other',
                         [('https://bunkr.cr/f/z-Q.jpg', 'z.jpg')], 1)

    run(bot._ingest_all(_channel_message(),
                        _sources(first) + _sources(second), [], 0))

    assert [m.text.splitlines()[0] for m in bot.log_channel.messages] == [
        '📚 <https://bunkr.cr/a/Qw3rTy12> “Holiday 2022”',
        '📚 <https://bunkr.cr/a/other> “Other”']
