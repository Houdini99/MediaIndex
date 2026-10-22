"""discord_bot: linking, who owns an ingest, and private ingest by DM.

Users link their Discord account by DMing the bot the `link CODE` line from
the index's /settings page; the bot redeems it with the sender's id, and
later asks the index (GET /api/discord/user) who a sender is. A member holding the
ingest role may also DM the bot; what they send is indexed *private* to
that linked account. Each of these is a privacy or access failure if it
regresses:

* the DM gate is the same role as the channel's, looked up in the ingest
  channel's server (a DM carries a User, which has no roles) — and it
  fails closed, including when the role gate is switched off;
* a private item always belongs to the sender, never the bot account: an
  unlinked sender is turned away before anything is downloaded, and an
  index that cannot be asked means "try again later", not "go ahead";
* nothing about a DM ingest reaches the shared bot-logs channel.

`config` is read at import time, so every test rebinds what it relies on.
"""
import asyncio
import types

import pytest

from bots_common.download import TooLarge
from bots_common.index_client import AuthError, IndexUnavailable, UploadResult
from bots_common.sniff import Blocked
from discord_bot import bot as botmod
from discord_bot import config

discord = botmod.discord


def run(coro):
    return asyncio.run(coro)


def role(rid, name):
    return types.SimpleNamespace(id=rid, name=name)


class FakeDM(discord.DMChannel):
    def __init__(self, cid=700):
        self.id = cid
        self.sent = []

    async def send(self, content, **kw):
        self.sent.append(content)


class FakeGuild:
    """fetch_member answers from `members`; a missing id is NotFound, an
    exception instance is raised as-is."""

    def __init__(self, members=None):
        self.members = members or {}
        self.lookups = []

    async def fetch_member(self, uid):
        self.lookups.append(uid)
        found = self.members.get(uid)
        if found is None:
            raise discord.NotFound()
        if isinstance(found, Exception):
            raise found
        return found


class FakeIndex:
    """The index as the bot sees it: who linked which Discord id, and an
    upload endpoint that records what it was asked to store."""

    def __init__(self, links=None, result=None):
        self.links = links if links is not None else {}
        self.result = result or UploadResult('ok', filehash='abc')
        self.lookup_error = None
        self.lookups = []
        self.uploads = []
        self.codes = {}               # code -> username it links to
        self.redeemed = []

    async def verify_discord_link(self, discord_id, code):
        self.redeemed.append((discord_id, code))
        if self.lookup_error is not None:
            raise self.lookup_error
        username = self.codes.pop(code, None)
        if username:
            self.links[discord_id] = username
        return username

    async def lookup_discord_user(self, discord_id):
        self.lookups.append(discord_id)
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.links.get(discord_id)

    async def upload(self, path, filename, tags, **kw):
        self.uploads.append(kw)
        return self.result


class FakeQueue:
    def __init__(self):
        self.calls = []

    async def schedule(self, channel_id, message_id, reason, attempts=0,
                       tags=None):
        self.calls.append(reason)
        return 60


def dm(author_id=42, content='', mid=500):
    return types.SimpleNamespace(
        id=mid, content=content, channel=FakeDM(),
        author=types.SimpleNamespace(id=author_id, display_name='alice'),
        jump_url='https://discord.test/dm',
        to_reference=lambda **kw: mid)


def channel_message(author_id=42, content='', mid=600):
    return types.SimpleNamespace(
        id=mid, content=content,
        channel=types.SimpleNamespace(id=config.INGEST_CHANNEL_ID),
        author=types.SimpleNamespace(id=author_id, display_name='alice'),
        jump_url='https://discord.test/channel')


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(config, 'INGEST_ROLE', 'Verified')
    monkeypatch.setattr(config, 'DM_INGEST', True)
    b = botmod.IndexBot.__new__(botmod.IndexBot)
    b.user = types.SimpleNamespace(id=1)
    b.index = FakeIndex(links={42: 'alice'})
    b._auth_backoff_until = 0.0
    b._retry = FakeQueue()
    b.guild = FakeGuild({42: types.SimpleNamespace(
        id=42, roles=[role(5, 'Verified')])})
    b.get_channel = lambda cid: (
        types.SimpleNamespace(id=cid, guild=b.guild)
        if cid == config.INGEST_CHANNEL_ID else None)
    b.handled, b.logs, b.reactions, b.downloads = [], [], [], []

    async def handle(message, **kw):
        b.handled.append(message)

    async def log(text):
        b.logs.append(text)

    async def react(message, emoji):
        b.reactions.append(emoji)

    async def unreact(message, emoji):
        pass

    async def download(att, dest):
        b.downloads.append(att.url)

    b._handle_message, b._log = handle, log
    b._react, b._unreact = react, unreact
    b._downloader = types.SimpleNamespace(download=download)
    return b


def handle(bot, message):
    """The real _handle_message (the fixture stubs it for the gate tests),
    with source discovery forbidden: these cases must stop before it."""
    async def gather(message):
        raise AssertionError("nothing may be gathered for this DM")

    bot._gather_sources = gather
    run(botmod.IndexBot._handle_message(bot, message))


def _att():
    return types.SimpleNamespace(filename='cat.png', size=0,
                                 url='https://cdn.test/cat.png')


# ── The DM gate ───────────────────────────────────────────────────────────
def test_a_role_holder_dm_is_ingested(bot):
    msg = dm()
    run(botmod.IndexBot.on_message(bot, msg))
    assert bot.handled == [msg]
    assert bot.guild.lookups == [42]


def test_the_role_is_looked_up_fresh_for_every_dm(bot):
    """No member cache: without the members intent it never hears that a
    role was taken away."""
    run(botmod.IndexBot.on_message(bot, dm(mid=1)))
    bot.guild.members[42] = types.SimpleNamespace(id=42, roles=[])
    run(botmod.IndexBot.on_message(bot, dm(mid=2)))
    assert [m.id for m in bot.handled] == [1]
    assert bot.guild.lookups == [42, 42]


def test_a_dm_without_the_role_is_ignored(bot):
    bot.guild.members[42] = types.SimpleNamespace(
        id=42, roles=[role(6, 'Member')])
    msg = dm()
    run(botmod.IndexBot.on_message(bot, msg))
    assert not bot.handled
    assert msg.channel.sent == []          # strangers get no reply
    assert bot.index.lookups == []         # nor an index lookup


def test_a_dm_from_someone_not_on_the_server_is_ignored(bot):
    run(botmod.IndexBot.on_message(bot, dm(author_id=77)))
    assert not bot.handled


def test_a_failed_member_lookup_fails_closed(bot):
    bot.guild.members[42] = discord.HTTPException()
    run(botmod.IndexBot.on_message(bot, dm()))
    assert not bot.handled


def test_dms_are_refused_while_the_role_gate_is_off(bot, monkeypatch):
    """An empty DISCORD_INGEST_ROLE leaves the *channel* open for an old
    deployment's sake. It must not open DMs to anyone sharing a server."""
    monkeypatch.setattr(config, 'INGEST_ROLE', '')
    run(botmod.IndexBot.on_message(bot, dm()))
    assert not bot.handled
    assert bot.guild.lookups == []


def test_dm_ingest_can_be_switched_off(bot, monkeypatch):
    monkeypatch.setattr(config, 'DM_INGEST', False)
    run(botmod.IndexBot.on_message(bot, dm()))
    assert not bot.handled


def test_a_channel_message_does_not_go_through_the_dm_gate(bot):
    msg = channel_message()
    msg.author.roles = [role(5, 'Verified')]
    run(botmod.IndexBot.on_message(bot, msg))
    assert bot.handled == [msg]
    assert bot.guild.lookups == []


# ── A DM needs a linked account ───────────────────────────────────────────
def test_an_unlinked_sender_is_told_how_to_link_and_nothing_is_fetched(bot):
    bot.index.links = {}
    msg = dm(content='https://example.com/cat.png')
    handle(bot, msg)
    assert len(msg.channel.sent) == 1
    assert '/settings' in msg.channel.sent[0]
    assert 'Get link code' in msg.channel.sent[0]
    assert bot.logs == []


def test_an_unreachable_index_queues_the_dm_instead_of_refusing_it(bot):
    """An outage is not a verdict on the sender: no "you're not linked"
    reply, and the message goes to the retry queue."""
    bot.index.lookup_error = IndexUnavailable("the index is unreachable")
    msg = dm()
    handle(bot, msg)
    assert bot.reactions == [botmod.ERROR, botmod.QUEUED]
    assert bot._retry.calls and 'unreachable' in bot._retry.calls[0]
    assert not any('/settings' in line for line in msg.channel.sent)
    assert bot.logs == []


def test_a_rejected_app_password_on_the_lookup_arms_the_backoff(bot):
    bot.index.lookup_error = AuthError("rejected")
    handle(bot, dm())
    assert bot._auth_backoff_until > 0
    assert bot.reactions[0] == botmod.BACKOFF


# ── Ownership in _ingest ──────────────────────────────────────────────────
def test_a_dm_upload_is_private_and_owned_by_the_sender(bot):
    msg = dm()
    outcome = run(bot._ingest(_att(), msg, ['cats']))
    assert outcome.emoji == botmod.OK
    assert bot.index.uploads == [{'uploaded_for': 'alice', 'private': True}]
    assert 'privately for **alice**' in msg.channel.sent[0]
    assert bot.logs == []


def test_a_channel_upload_is_public_and_owned_by_the_linked_user(bot):
    run(bot._ingest(_att(), channel_message(), []))
    assert bot.index.uploads == [{'uploaded_for': 'alice', 'private': False}]
    assert bot.logs and 'privately' not in bot.logs[0]


def test_an_unlinked_channel_poster_stays_owned_by_the_bot(bot):
    bot.index.links = {}
    outcome = run(bot._ingest(_att(), channel_message(), []))
    assert outcome.emoji == botmod.OK
    assert bot.index.uploads == [{'uploaded_for': None, 'private': False}]


def test_a_dm_whose_link_vanished_mid_flight_is_not_stored(bot):
    """_handle_message checked the link, but it can be removed while a
    forward waits for its tags — the private upload must not fall back to
    the bot account."""
    bot.index.links = {}
    msg = dm()
    outcome = run(bot._ingest(_att(), msg, []))
    assert outcome.emoji == botmod.UNSUPPORTED
    assert bot.index.uploads == [] and bot.downloads == []
    assert '/settings' in msg.channel.sent[0]


def test_an_owner_lookup_outage_is_retryable_and_downloads_nothing(bot):
    bot.index.lookup_error = IndexUnavailable("the index returned HTTP 502")
    outcome = run(bot._ingest(_att(), channel_message(), []))
    assert outcome.emoji == botmod.ERROR and outcome.retryable
    assert bot.downloads == [] and bot.index.uploads == []


# ── Refused downloads ─────────────────────────────────────────────────────
def _failing_download(bot, error):
    async def download(att, dest):
        raise error
    bot._downloader = types.SimpleNamespace(download=download)


def test_a_link_to_an_internal_host_is_refused_and_says_why(bot):
    _failing_download(bot, Blocked(
        "web is not a public address, so I won't fetch it"))
    outcome = run(bot._ingest(_att(), channel_message(), []))
    assert outcome.emoji == botmod.UNSUPPORTED and not outcome.retryable
    assert bot.index.uploads == []
    assert 'not a public address' in bot.logs[0]


def test_a_link_over_the_cap_is_refused_with_both_sizes(bot, monkeypatch):
    monkeypatch.setattr(config, 'LINK_MAX_BYTES', 512_000_000)
    _failing_download(bot, TooLarge(900_000_000))
    outcome = run(bot._ingest(_att(), channel_message(), []))
    assert outcome.emoji == botmod.UNSUPPORTED and not outcome.retryable
    assert bot.index.uploads == []
    assert '(900 MB) is over the 512 MB link limit' in bot.logs[0]


# ── Where status lines go ─────────────────────────────────────────────────
def test_a_dm_failure_is_reported_in_the_dm_not_the_log_channel(bot):
    bot.index.result = UploadResult('error', 'the index said no')
    msg = dm()
    outcome = run(bot._ingest(_att(), msg, []))
    assert outcome.emoji == botmod.ERROR
    assert 'the index said no' in msg.channel.sent[0]
    assert bot.logs == []


def test_a_channel_line_still_goes_to_bot_logs_with_its_link(bot):
    run(bot._report(channel_message(), "✅ done"))
    assert bot.logs == ["✅ done\nhttps://discord.test/channel"]


def test_a_dm_line_has_no_jump_link(bot):
    msg = dm()
    run(bot._report(msg, "✅ done"))
    assert msg.channel.sent == ["✅ done"]


# ── Forward tags stay in the conversation they were typed in ──────────────
def test_tags_typed_in_a_dm_do_not_tag_channel_forwards(bot):
    bot._pending_forwards = {}
    entry = botmod._PendingForward()
    bot._pending_forwards[(config.INGEST_CHANNEL_ID, 42)] = [entry]

    run(bot._apply_pending_tags(dm(content='cats,funny')))
    assert entry.tags == [] and not entry.event.is_set()

    run(bot._apply_pending_tags(channel_message(content='cats,funny')))
    assert entry.tags == ['cats', 'funny'] and entry.event.is_set()


# ── `link CODE` ───────────────────────────────────────────────────────────
def test_a_link_code_links_the_sender_and_says_so(bot):
    bot.index.links = {}
    bot.index.codes = {'K7QX-M2PA': 'alice'}
    msg = dm(content='link K7QX-M2PA')
    run(botmod.IndexBot.on_message(bot, msg))
    assert bot.index.redeemed == [(42, 'K7QX-M2PA')]
    assert 'Linked to index account **alice**' in msg.channel.sent[0]
    assert not bot.handled                     # never treated as ingest


def test_linking_works_without_the_role_and_with_dm_ingest_off(bot,
                                                                monkeypatch):
    """A link also decides who owns channel posts, so it sits ahead of the
    DM gate — and costs no member lookup."""
    monkeypatch.setattr(config, 'DM_INGEST', False)
    bot.guild.members = {}
    bot.index.codes = {'K7QX-M2PA': 'alice'}
    msg = dm(author_id=77, content='link K7QX-M2PA')
    run(botmod.IndexBot.on_message(bot, msg))
    assert bot.index.links[77] == 'alice'
    assert bot.guild.lookups == []
    assert msg.channel.sent[0].endswith('credited to you.')


def test_a_wrong_code_is_refused(bot):
    msg = dm(content='link AAAA-AAAA')
    run(botmod.IndexBot.on_message(bot, msg))
    assert 'wrong, already used, or expired' in msg.channel.sent[0]


def test_an_unreachable_index_asks_to_resend_the_same_code(bot):
    bot.index.lookup_error = IndexUnavailable("the index is unreachable")
    msg = dm(content='link K7QX-M2PA')
    run(botmod.IndexBot.on_message(bot, msg))
    assert 'same code again' in msg.channel.sent[0]


def test_a_bare_link_explains_where_codes_come_from(bot):
    msg = dm(content='link')
    run(botmod.IndexBot.on_message(bot, msg))
    assert bot.index.redeemed == []
    assert 'Get link code' in msg.channel.sent[0]


def test_link_followed_by_a_url_is_still_an_ingest(bot):
    msg = dm(content='link https://youtu.be/abc')
    run(botmod.IndexBot.on_message(bot, msg))
    assert bot.index.redeemed == []
    assert bot.handled == [msg]


def test_a_link_code_in_the_channel_is_not_redeemed(bot):
    """Codes are only taken by DM. In the channel anyone could read it and
    redeem it first, linking *their* Discord account to the poster's."""
    msg = channel_message(content='link K7QX-M2PA')
    msg.author.roles = [role(5, 'Verified')]
    run(botmod.IndexBot.on_message(bot, msg))
    assert bot.index.redeemed == []
