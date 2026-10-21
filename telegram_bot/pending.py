"""Redis-backed store for media that arrived without tags.

The bot refuses to ingest untagged media, so something has to hold the
item between "here is a photo" and "here are its tags". What is held is
the Telegram **file_id** — or, for a link, the **URL** — never the bytes:
a file_id stays valid for the bot indefinitely, so a waiting item costs a
few hundred bytes of Redis instead of a temp file, nothing is downloaded
for an item the user abandons, and there is no orphaned-file reaper to
write.

(The Discord bot downloads first for the opposite reason — its CDN links
are signed and expire — so the two bots differ here on purpose.)

One Redis hash per user, `telegram:pending:<telegram_user_id>`, keyed by
the message id the item arrived on so a reply can target exactly one
entry (see `add` for the one case that needs a finer key). The hash
carries a TTL, refreshed on write: a user who never sends tags is
forgotten rather than accumulating forever.

State also survives a bot restart this way, which matters — a redeploy in
the seconds between a photo and its tags would otherwise silently drop it.
"""
import json
import logging
import time

import redis.asyncio as aioredis

from . import config


class PendingStore:
    def __init__(self, url=None, ttl=None, limit=None, client=None):
        # `client` exists so tests can drive this against an in-memory
        # double: redis is not reachable from the test suite, and the
        # eviction, drain and throttle logic here is worth covering
        # without one.
        self._redis = client or aioredis.from_url(
            url or config.REDIS_URL, decode_responses=True)
        self._ttl = ttl if ttl is not None else config.PENDING_TTL
        self._limit = limit if limit is not None else config.PENDING_MAX

    async def close(self):
        await self._redis.aclose()

    @staticmethod
    def _key(user_id):
        return f"{config.PENDING_KEY}:{user_id}"

    async def add(self, user_id, key, entry):
        """Park one untagged item under `key`. Returns the number waiting.

        `key` is normally the id of the message the item arrived on, which
        is what lets a reply target it directly. One message can carry
        several links, though, so those are keyed `<message_id>#<n>` to
        stop them overwriting each other — and the key is written into the
        entry, so an item matched by *message* can still be taken by its
        own field.

        Over the per-user cap the oldest entries are evicted rather than
        the newest refused: the cap exists so one user cannot grow the
        store without bound, and silently dropping what they just sent
        while keeping something from yesterday would be the more
        surprising of the two failures.
        """
        hash_key = self._key(user_id)
        entry = dict(entry, ts=time.time(), key=str(key))
        pipe = self._redis.pipeline()
        pipe.hset(hash_key, str(key), json.dumps(entry))
        pipe.expire(hash_key, self._ttl)
        pipe.hlen(hash_key)
        count = (await pipe.execute())[-1]
        if count > self._limit:
            await self._evict_oldest(hash_key, count - self._limit)
            count = self._limit
        return count

    async def _evict_oldest(self, key, how_many):
        raw = await self._redis.hgetall(key)
        entries = sorted(
            ((field, self._load(value)) for field, value in raw.items()),
            key=lambda pair: (pair[1] or {}).get('ts', 0))
        stale = [field for field, _ in entries[:how_many]]
        if stale:
            await self._redis.hdel(key, *stale)
            logging.info("pending: evicted %d oldest entries for %s",
                         len(stale), key)

    @staticmethod
    def _load(value):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    async def count(self, user_id):
        return await self._redis.hlen(self._key(user_id))

    async def list(self, user_id):
        """Every waiting entry for a user, oldest first."""
        raw = await self._redis.hgetall(self._key(user_id))
        entries = [e for e in (self._load(v) for v in raw.values()) if e]
        return sorted(entries, key=lambda e: e.get('ts', 0))

    async def take(self, user_id, key):
        """Remove and return one entry by its key (see `add`).

        The delete decides the winner: two tag messages racing on the same
        item both read it, but only the one whose HDEL reports a removal
        may ingest it, so a double-send cannot double-upload.
        """
        hash_key, field = self._key(user_id), str(key)
        raw = await self._redis.hget(hash_key, field)
        if raw is None:
            return None
        if not await self._redis.hdel(hash_key, field):
            return None
        return self._load(raw)

    async def drain(self, user_id):
        """Remove and return every waiting entry for a user, oldest first.

        Atomic against a concurrent drain for the same reason `take` is:
        the whole hash is renamed out of the way in one step, so a second
        caller finds nothing rather than a half-emptied hash.
        """
        key = self._key(user_id)
        scratch = f"{key}:draining:{int(time.time() * 1000)}"
        try:
            await self._redis.rename(key, scratch)
        except aioredis.ResponseError:
            return []          # no such key — nothing was waiting
        raw = await self._redis.hgetall(scratch)
        await self._redis.delete(scratch)
        entries = [e for e in (self._load(v) for v in raw.values()) if e]
        return sorted(entries, key=lambda e: e.get('ts', 0))

    async def clear(self, user_id):
        """Drop everything waiting for a user; returns how many there were."""
        entries = await self.drain(user_id)
        return len(entries)

    # ── Per-user flags (the /private toggle) ───────────────────────────
    async def set_flag(self, user_id, name, value):
        key = f"{config.FLAG_KEY}:{user_id}"
        if value:
            await self._redis.hset(key, name, '1')
        else:
            await self._redis.hdel(key, name)

    async def get_flag(self, user_id, name):
        return bool(await self._redis.hget(f"{config.FLAG_KEY}:{user_id}", name))

    # ── Notice throttle (unlinked senders) ─────────────────────────────
    async def claim_notice(self, user_id, every=None):
        """True at most once per `every` seconds for a given user.

        SET NX EX: the key's own existence is the throttle, so nothing has
        to be read, compared and written back, and two messages arriving
        together cannot both win.
        """
        every = every if every is not None else config.UNLINKED_NOTICE_EVERY
        return bool(await self._redis.set(
            f"{config.NOTICE_KEY}:{user_id}", '1', nx=True, ex=int(every)))
