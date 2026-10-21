"""Redis-backed retry queue for ingests that failed for a transient reason.

Without it, a ⏳ (index login backing off) or ❌ (network hiccup, source
5xx) was terminal: the media was dropped and the only recovery was for a
human to notice the emoji and repost. With the index's auth backoff
defaulting to 15 minutes, one expired password quietly lost every upload
in that window.

What is stored is the *message reference*, not the media: re-fetching the
message re-derives its attachments, links and embeds, and hands back
freshly signed Discord CDN URLs for free. The cost is that a partially
successful message is re-processed whole — sources that already landed
come back as duplicates, which the index detects by hash and the bot
reports as ♻️. That is the right trade: a duplicate is free, a lost
upload is not.

Redis holds two keys, so a restart resumes the queue:

* a sorted set (member ``"<channel_id>:<message_id>"``, score = the unix
  time the next attempt is due) — ZADD re-scores an existing member, so a
  message can never be queued twice;
* a hash of the same members to their JSON state (attempt count, the
  reason last time, when it was first queued).

Every method is best-effort: Redis being down must never break ingestion,
so failures are logged and reported as "nothing queued" / "nothing due".
"""
import json
import logging
import time

import redis.asyncio as aioredis


class RetryQueue:
    def __init__(self, url, key='discord:ingest_retry', max_attempts=5,
                 base_delay=60.0, max_delay=3600.0):
        self._url = url
        self._key = key
        self._meta_key = f'{key}:state'
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._redis = None

    def _r(self):
        if self._redis is None:
            self._redis = aioredis.from_url(self._url)
        return self._redis

    async def close(self):
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @staticmethod
    def _member(channel_id, message_id):
        return f'{channel_id}:{message_id}'

    def delay_for(self, attempts):
        """Exponential backoff, capped: 60s, 2m, 4m, 8m, … up to max_delay.
        `attempts` is how many have already been made."""
        return min(self._base_delay * (2 ** max(0, attempts)), self._max_delay)

    async def schedule(self, channel_id, message_id, reason, attempts=0,
                       tags=None):
        """Queue (or re-queue) a message for another attempt.

        `tags` is the tag list that was in effect for this message; it
        rides along so a forward's tags — which arrived as a separate
        follow-up message that will not be sent again — survive across
        attempts instead of being re-derived from a message that no longer
        has them.

        Returns the delay in seconds before it comes due, or None when the
        attempt budget is spent (or Redis is unreachable) — the caller
        should then report a final failure.
        """
        if attempts >= self._max_attempts:
            return None
        delay = self.delay_for(attempts)
        member = self._member(channel_id, message_id)
        state = json.dumps({'channel_id': channel_id,
                            'message_id': message_id,
                            'attempts': attempts,
                            'reason': reason,
                            'tags': list(tags or []),
                            'queued_at': time.time()})
        try:
            pipe = self._r().pipeline()
            pipe.zadd(self._key, {member: time.time() + delay})
            pipe.hset(self._meta_key, member, state)
            await pipe.execute()
        except Exception:
            logging.exception("retry: could not queue %s", member)
            return None
        return delay

    async def due(self, limit=10):
        """Claim up to `limit` entries whose next attempt is due.

        Claiming removes an entry from Redis before returning it, so a
        poison entry can never be redelivered in a loop; the caller is
        expected to re-schedule it if the attempt itself fails. Returns a
        list of state dicts, with 'attempts' already incremented to count
        the attempt about to be made.
        """
        try:
            members = await self._r().zrangebyscore(
                self._key, '-inf', time.time(), start=0, num=limit)
        except Exception:
            logging.exception("retry: could not read the queue")
            return []
        entries = []
        for raw in members:
            member = raw.decode() if isinstance(raw, bytes) else raw
            try:
                pipe = self._r().pipeline()
                pipe.hget(self._meta_key, member)
                pipe.zrem(self._key, member)
                pipe.hdel(self._meta_key, member)
                state, _, _ = await pipe.execute()
            except Exception:
                logging.exception("retry: could not claim %s", member)
                continue
            if not state:
                continue
            try:
                entry = json.loads(state)
            except ValueError:
                logging.warning("retry: dropping unreadable state for %s",
                                member)
                continue
            entry['attempts'] = entry.get('attempts', 0) + 1
            entry['max_attempts'] = self._max_attempts
            entries.append(entry)
        return entries

    async def pending(self):
        """How many messages are waiting (0 when Redis is unreachable)."""
        try:
            return await self._r().zcard(self._key)
        except Exception:
            logging.exception("retry: could not size the queue")
            return 0
