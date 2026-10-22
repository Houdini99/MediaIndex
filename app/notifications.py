"""Best-effort event publishing for the Discord bot via a Redis list.

New public media is announced by LPUSHing a JSON event that the bot
container consumes with BRPOP (oldest first). The list is capped so an
offline bot can never grow it unboundedly, and every path here is
wrapped: publishing must NEVER break an upload.
"""
import json
import logging
import os

from .config import Config

EVENTS_KEY = 'discord:media_events'
MAX_QUEUE = 1000

_client = None


def _get_client():
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(Config.REDIS_URL)
    return _client


def build_media_event(filepath, thumb, filehash, tags, uid, upload_date):
    """Assemble a media_added payload, or None on any failure.

    `tags` should be the same string handed to insert_media(); the
    user:<name> tag is added here the same way insert_media adds it.
    """
    try:
        # Local imports: app.media.routes imports this module, so pulling
        # the media package in at import time is circular.
        from .media.db import get_username_by_id
        from .media.tagging import tag_str, with_user_tag
        uname = get_username_by_id(uid)
        # The same helper as insert_media, so the announced tags are
        # byte-identical to the stored ones.
        tags = tag_str(with_user_tag(tags, uname))
        return {
            'event': 'media_added',
            'filepath': os.path.basename(filepath),
            'thumb': os.path.basename(thumb) if thumb else None,
            'filehash': filehash,
            'tags': tags,
            'uploaded_by': uname or 'Unknown',
            'upload_date': upload_date,
        }
    except Exception:
        logging.exception("notifications: building event failed")
        return None


def publish_event(event):
    """Push an event onto the bot queue; never raises.

    No-op when REDIS_URL is not a real Redis URL (tests run with
    REDIS_URL=memory://, which only Flask-Limiter understands).
    """
    if not event:
        return
    if not Config.REDIS_URL.startswith(('redis://', 'rediss://', 'unix://')):
        return
    try:
        pipe = _get_client().pipeline()
        pipe.lpush(EVENTS_KEY, json.dumps(event))
        pipe.ltrim(EVENTS_KEY, 0, MAX_QUEUE - 1)
        pipe.execute()
    except Exception:
        logging.exception("notifications: publish failed (upload unaffected)")
