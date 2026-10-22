"""discord_bot/bot.py: the two decisions that are easy to get wrong and
invisible until they bite —

* which failures are worth retrying (and how the attempt budget is spent),
* what a duplicate report is allowed to say.

discord.py and aiohttp are faked in conftest; the logic under test is the
bot's own, so nothing here touches a gateway.
"""
import asyncio
import types

import pytest

from bots_common.index_client import UploadResult
from discord_bot import bot as botmod


def run(coro):
    return asyncio.run(coro)


class FakeQueue:
    """Records what the bot asks the retry queue to do."""

    def __init__(self, delay=60):
        self.calls = []
        self.delay = delay

    async def schedule(self, channel_id, message_id, reason, attempts=0,
                       tags=None):
        self.calls.append({'channel_id': channel_id,
                           'message_id': message_id, 'reason': reason,
                           'attempts': attempts, 'tags': tags})
        return self.delay


class FakeMessage:
    def __init__(self):
        self.id = 200
        self.channel = types.SimpleNamespace(id=100)
        self.jump_url = 'https://discord.test/jump'


@pytest.fixture
def bot():
    """An IndexBot with only the collaborators _settle touches."""
    b = botmod.IndexBot.__new__(botmod.IndexBot)
    b._retry = FakeQueue()
    b.reactions, b.removed, b.logs = [], [], []

    async def react(message, emoji):
        b.reactions.append(emoji)

    async def unreact(message, emoji):
        b.removed.append(emoji)

    async def log(text):
        b.logs.append(text)

    b._react, b._unreact, b._log = react, unreact, log
    return b


# ── Retry decisions ───────────────────────────────────────────────────────
def test_settled_outcomes_queue_nothing(bot):
    outcomes = [botmod._Outcome(botmod.OK),
                botmod._Outcome(botmod.DUPLICATE),
                botmod._Outcome(botmod.UNSUPPORTED)]
    run(bot._settle(FakeMessage(), outcomes, 0, []))
    assert bot._retry.calls == []
    assert bot.removed == [botmod.QUEUED]     # clears a stale marker
    assert bot.logs == []


def test_transient_failure_is_queued_with_the_message_reference(bot):
    outcome = botmod._Outcome(botmod.ERROR, True, 'network error')
    run(bot._settle(FakeMessage(), [outcome], 0, ['cats']))
    call = bot._retry.calls[0]
    assert call['channel_id'] == 100 and call['message_id'] == 200
    assert call['attempts'] == 0
    assert call['reason'] == 'network error'
    assert call['tags'] == ['cats']
    assert botmod.QUEUED in bot.reactions
    assert 'retrying in 1m' in bot.logs[0]


def test_tags_ride_along_so_a_forward_keeps_them(bot):
    """A forward's tags arrive as a separate follow-up message that will
    never be sent again — losing them on retry would silently drop them."""
    outcome = botmod._Outcome(botmod.BACKOFF, True, 'backing off')
    run(bot._settle(FakeMessage(), [outcome], 2, ['cats', 'funny']))
    assert bot._retry.calls[0]['tags'] == ['cats', 'funny']
    assert bot._retry.calls[0]['attempts'] == 2


def test_one_retryable_source_among_many_still_queues(bot):
    outcomes = [botmod._Outcome(botmod.OK),
                botmod._Outcome(botmod.ERROR, True, 'network error'),
                botmod._Outcome(botmod.ERROR, True, 'source HTTP 503')]
    run(bot._settle(FakeMessage(), outcomes, 0, []))
    assert len(bot._retry.calls) == 1
    assert bot._retry.calls[0]['reason'] == 'network error (+1 more)'


def test_exhausted_budget_reports_giving_up(bot):
    bot._retry.delay = None                   # the queue refuses to re-queue
    outcome = botmod._Outcome(botmod.ERROR, True, 'network error')
    run(bot._settle(FakeMessage(), [outcome], 5, []))
    assert botmod.QUEUED not in bot.reactions
    assert bot.removed == [botmod.QUEUED]
    assert 'giving up after 6 attempt(s)' in bot.logs[0]
    assert 'network error' in bot.logs[0]


def test_retries_disabled_queues_nothing(bot):
    bot._retry = None
    outcome = botmod._Outcome(botmod.ERROR, True, 'network error')
    run(bot._settle(FakeMessage(), [outcome], 0, []))
    assert bot.logs == [] and botmod.QUEUED not in bot.reactions


# ── Duplicate reporting ───────────────────────────────────────────────────
def _note(duplicate):
    return botmod.IndexBot._duplicate_note(
        UploadResult('duplicate', duplicate=duplicate))


def test_duplicate_note_names_and_links_the_item(monkeypatch):
    monkeypatch.setattr(botmod.config, 'INDEX_PUBLIC_URL',
                        'https://media.example.com')
    note = _note({'id': 42, 'is_private': False, 'filepath': 'abc.png',
                  'tags': 'cats,user:bob', 'uploaded_by': 'bob'})
    assert '[#42](https://media.example.com/media/abc.png)' in note
    assert '**bob**' in note
    assert '[cats](https://media.example.com/?search=cats)' in note
    # the uploader is already named — the user: tag would be noise
    assert 'user:bob' not in note


def test_duplicate_note_without_tags_omits_the_tag_row(monkeypatch):
    monkeypatch.setattr(botmod.config, 'INDEX_PUBLIC_URL', 'https://x.test')
    note = _note({'id': 7, 'is_private': False, 'filepath': 'a.mp4',
                  'tags': '', 'uploaded_by': 'bob'})
    assert note.endswith('**bob**') and 'tags:' not in note


def test_duplicate_note_hides_everything_about_a_private_item():
    """Re-posting a file that collides with someone's private upload must
    not reveal its tags or its owner to the channel."""
    note = _note({'id': 9, 'is_private': True})
    assert note == 'already indexed as a private item (#9)'


def test_duplicate_note_falls_back_when_the_index_named_nothing():
    """HTML upload mode cannot identify the colliding item at all."""
    assert _note(None) == 'already indexed (duplicate)'
    assert _note({'id': 5}) == 'already indexed (duplicate)'
