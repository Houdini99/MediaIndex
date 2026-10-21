"""discord_bot/retry.py: the queue that keeps a transiently failed ingest
alive instead of dropping it.

A fake Redis stands in for the server, so the tests exercise the real key
layout (sorted set + state hash) offline.
"""
import asyncio
import json

import pytest

from discord_bot import retry


class FakeRedis:
    """Just enough redis.asyncio for RetryQueue: a sorted set, a hash, and
    a pipeline that queues coroutine-free commands and applies them on
    execute()."""

    def __init__(self, fail=False):
        self.z = {}          # member -> score
        self.h = {}          # member -> state json
        self.fail = fail
        self.closed = False

    def _check(self):
        if self.fail:
            raise RuntimeError("redis is down")

    # ── direct commands ────────────────────────────────────────────────
    async def zrangebyscore(self, key, lo, hi, start=0, num=None):
        self._check()
        due = sorted((s, m) for m, s in self.z.items() if s <= hi)
        members = [m.encode() for _, m in due][start:]
        return members[:num] if num is not None else members

    async def zcard(self, key):
        self._check()
        return len(self.z)

    async def aclose(self):
        self.closed = True

    # ── pipeline ───────────────────────────────────────────────────────
    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def zadd(self, key, mapping):
        self.ops.append(lambda: self.r.z.update(mapping))

    def hset(self, key, field, value):
        self.ops.append(lambda: self.r.h.__setitem__(field, value))

    def hget(self, key, field):
        self.ops.append(lambda: self.r.h.get(field))

    def zrem(self, key, member):
        self.ops.append(lambda: self.r.z.pop(member, None))

    def hdel(self, key, field):
        self.ops.append(lambda: self.r.h.pop(field, None))

    async def execute(self):
        self.r._check()
        return [op() for op in self.ops]


@pytest.fixture
def queue():
    q = retry.RetryQueue('redis://fake', max_attempts=3, base_delay=60,
                         max_delay=600)
    q._redis = FakeRedis()
    return q


def run(coro):
    return asyncio.run(coro)


# ── Backoff schedule ──────────────────────────────────────────────────────
def test_delay_doubles_then_caps(queue):
    assert [queue.delay_for(i) for i in range(5)] == [60, 120, 240, 480, 600]


# ── Scheduling ────────────────────────────────────────────────────────────
def test_schedule_stores_state_and_returns_the_delay(queue):
    delay = run(queue.schedule(1, 2, 'network error', attempts=0,
                               tags=['cats']))
    assert delay == 60
    assert list(queue._redis.z) == ['1:2']
    state = json.loads(queue._redis.h['1:2'])
    assert state['channel_id'] == 1 and state['message_id'] == 2
    assert state['attempts'] == 0
    assert state['reason'] == 'network error'
    assert state['tags'] == ['cats']


def test_rescheduling_the_same_message_does_not_duplicate_it(queue):
    run(queue.schedule(1, 2, 'first', attempts=0))
    run(queue.schedule(1, 2, 'second', attempts=1))
    assert list(queue._redis.z) == ['1:2']
    assert json.loads(queue._redis.h['1:2'])['reason'] == 'second'
    assert run(queue.pending()) == 1


def test_schedule_refuses_once_the_budget_is_spent(queue):
    assert run(queue.schedule(1, 2, 'boom', attempts=3)) is None
    assert run(queue.schedule(1, 2, 'boom', attempts=99)) is None
    assert queue._redis.z == {}


def test_redis_failure_is_reported_as_not_queued(queue):
    queue._redis.fail = True
    assert run(queue.schedule(1, 2, 'boom')) is None
    assert run(queue.due()) == []
    assert run(queue.pending()) == 0


# ── Draining ──────────────────────────────────────────────────────────────
def test_due_ignores_entries_that_are_not_ready_yet(queue):
    run(queue.schedule(1, 2, 'later'))          # +60s
    assert run(queue.due()) == []
    assert run(queue.pending()) == 1


def test_due_claims_ready_entries_and_increments_attempts(queue):
    run(queue.schedule(1, 2, 'network error', attempts=1, tags=['cats']))
    queue._redis.z['1:2'] = 0                   # make it due
    entries = run(queue.due())
    assert len(entries) == 1
    entry = entries[0]
    assert entry['message_id'] == 2
    assert entry['attempts'] == 2               # counts the attempt about to run
    assert entry['tags'] == ['cats']
    assert entry['max_attempts'] == 3


def test_claiming_removes_the_entry_from_redis(queue):
    run(queue.schedule(1, 2, 'boom'))
    queue._redis.z['1:2'] = 0
    assert len(run(queue.due())) == 1
    assert queue._redis.z == {} and queue._redis.h == {}
    assert run(queue.due()) == []               # not redelivered


def test_due_honours_its_limit(queue):
    for mid in range(5):
        run(queue.schedule(1, mid, 'boom'))
        queue._redis.z[f'1:{mid}'] = 0
    assert len(run(queue.due(limit=2))) == 2
    assert run(queue.pending()) == 3


def test_unreadable_state_is_dropped_not_raised(queue):
    run(queue.schedule(1, 2, 'boom'))
    queue._redis.z['1:2'] = 0
    queue._redis.h['1:2'] = b'{not json'
    assert run(queue.due()) == []


def test_close_releases_the_client(queue):
    fake = queue._redis
    run(queue.close())
    assert fake.closed and queue._redis is None
