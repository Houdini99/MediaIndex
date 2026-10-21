"""telegram_bot/pending.py: the store that holds untagged media.

Because the bot refuses to index untagged media, this store sits directly
on the critical path — an item lost here is an item the user sent and
never got. The behaviours worth pinning are the ones that are easy to get
subtly wrong: an item may be ingested exactly once even if two tag
messages race, a drain must not half-empty the hash, and one user must not
be able to grow the store without bound.

Redis is faked (see test_telegram_ingest.FakeRedis) — this is about the
store's own logic, not about Redis.
"""
import asyncio

import pytest

from telegram_bot.bot import IndexBot
from telegram_bot.pending import PendingStore
from test_telegram_ingest import FakeRedis


def run(coro):
    return asyncio.run(coro)


def entry(name='pic.jpg', file_id='f1'):
    return {'file_id': file_id, 'filename': name, 'ext': 'jpg', 'size': 10,
            'prompt_message_id': None, 'private': False}


@pytest.fixture()
def store():
    return PendingStore(client=FakeRedis(), ttl=60, limit=3)


def test_add_and_list_are_oldest_first(store):
    for i in range(3):
        run(store.add(1, 100 + i, entry(f'{i}.jpg')))
    names = [e['filename'] for e in run(store.list(1))]
    assert names == ['0.jpg', '1.jpg', '2.jpg']


def test_take_removes_exactly_one(store):
    run(store.add(1, 100, entry('a.jpg')))
    run(store.add(1, 101, entry('b.jpg')))
    assert run(store.take(1, 100))['filename'] == 'a.jpg'
    assert run(store.count(1)) == 1


def test_take_is_a_one_time_claim(store):
    """Two tag messages racing on one item must not upload it twice — the
    delete decides the winner, not the read."""
    run(store.add(1, 100, entry()))
    assert run(store.take(1, 100)) is not None
    assert run(store.take(1, 100)) is None


def test_take_of_something_never_parked_is_none(store):
    assert run(store.take(1, 999)) is None


def test_drain_empties_the_store(store):
    run(store.add(1, 100, entry('a.jpg')))
    run(store.add(1, 101, entry('b.jpg')))
    drained = run(store.drain(1))
    assert [e['filename'] for e in drained] == ['a.jpg', 'b.jpg']
    assert run(store.count(1)) == 0


def test_a_second_drain_finds_nothing(store):
    """The hash is renamed out of the way in one step, so a concurrent
    drain sees nothing rather than a half-emptied hash."""
    run(store.add(1, 100, entry()))
    assert len(run(store.drain(1))) == 1
    assert run(store.drain(1)) == []


def test_drain_of_an_empty_store_is_not_an_error(store):
    assert run(store.drain(7)) == []


def test_users_do_not_see_each_others_items(store):
    run(store.add(1, 100, entry('mine.jpg')))
    run(store.add(2, 100, entry('theirs.jpg')))
    assert [e['filename'] for e in run(store.list(1))] == ['mine.jpg']
    assert run(store.count(2)) == 1


def test_the_oldest_entries_are_evicted_over_the_cap(store):
    """The cap stops one user growing the store without bound. Evicting
    the oldest rather than refusing the newest is deliberate: silently
    dropping what someone just sent, while keeping something from
    yesterday, is the more surprising failure."""
    for i in range(5):
        run(store.add(1, 100 + i, entry(f'{i}.jpg')))
    names = [e['filename'] for e in run(store.list(1))]
    assert names == ['2.jpg', '3.jpg', '4.jpg']
    assert run(store.count(1)) == 3


def test_flags_round_trip_and_clear(store):
    assert run(store.get_flag(1, 'private')) is False
    run(store.set_flag(1, 'private', True))
    assert run(store.get_flag(1, 'private')) is True
    run(store.set_flag(1, 'private', False))
    assert run(store.get_flag(1, 'private')) is False


def test_the_notice_throttle_fires_once(store):
    assert run(store.claim_notice(1, every=60)) is True
    assert run(store.claim_notice(1, every=60)) is False
    assert run(store.claim_notice(2, every=60)) is True


def test_clear_reports_how_many_were_dropped(store):
    run(store.add(1, 100, entry()))
    run(store.add(1, 101, entry()))
    assert run(store.clear(1)) == 2
    assert run(store.clear(1)) == 0


# ── Duplicate reporting must not leak private items ───────────────────────
def _result(duplicate):
    class R:
        pass
    r = R()
    r.duplicate = duplicate
    return r


def test_a_private_duplicate_reveals_nothing_but_its_existence():
    """The index withholds a private item's tags and owner on purpose; the
    bot must not become the side channel that hands them back."""
    note = IndexBot._duplicate_note(_result({'id': 7, 'is_private': True}))
    assert 'private' in note
    assert 'tags' not in note


def test_a_public_duplicate_names_what_it_collided_with():
    note = IndexBot._duplicate_note(_result(
        {'id': 7, 'is_private': False, 'tags': 'cats,funny',
         'uploaded_by': 'bob'}))
    assert 'bob' in note and 'cats, funny' in note


def test_an_unidentifiable_duplicate_still_reports_cleanly():
    assert 'already in the index' in IndexBot._duplicate_note(_result(None))
