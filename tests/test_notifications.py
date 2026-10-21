"""Discord event publishing: fired for public uploads, skipped for private
ones, deferred for videos, and never able to break an upload."""
import io
import json

import pytest
from PIL import Image

from app.config import Config
from app import notifications
from app.media.db import get_all_media_paths


def _png_bytes(color):
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buf, format='PNG')
    return buf.getvalue()


def _upload(client, data, name='pic.png', private=False):
    payload = {'files': (io.BytesIO(data), name), 'tags_0': 'test'}
    if private:
        payload['private_0'] = '1'
    return client.post('/upload', data=payload,
                       content_type='multipart/form-data', follow_redirects=True)


class FakePipeline:
    def __init__(self, events, fail=False):
        self._events = events
        self._fail = fail
        self._pending = []

    def lpush(self, key, value):
        self._pending.append((key, json.loads(value)))

    def ltrim(self, key, start, stop):
        pass

    def execute(self):
        if self._fail:
            raise ConnectionError("redis down")
        self._events.extend(self._pending)


class FakeRedis:
    def __init__(self, fail=False):
        self.events = []
        self._fail = fail

    def pipeline(self):
        return FakePipeline(self.events, fail=self._fail)


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(Config, 'REDIS_URL', 'redis://fake:6379')
    monkeypatch.setattr(notifications, '_get_client', lambda: fake)
    return fake


def test_public_upload_publishes_event(client, logged_in, user, fake_redis):
    resp = _upload(client, _png_bytes('red'))
    assert 'Successful'.encode() in resp.data
    assert len(fake_redis.events) == 1
    key, event = fake_redis.events[0]
    assert key == notifications.EVENTS_KEY
    _, fp, th = get_all_media_paths()[0]
    assert event['event'] == 'media_added'
    assert event['filepath'] == fp
    assert event['thumb'] == th
    assert event['uploaded_by'] == user[1]
    assert 'test' in event['tags'] and f'user:{user[1]}' in event['tags']


def test_private_upload_publishes_nothing(client, logged_in, fake_redis):
    resp = _upload(client, _png_bytes('blue'), private=True)
    assert 'Successful'.encode() in resp.data
    assert fake_redis.events == []


def test_duplicate_upload_publishes_once(client, logged_in, fake_redis):
    data = _png_bytes('green')
    _upload(client, data)
    _upload(client, data, name='again.png')
    assert len(fake_redis.events) == 1


def test_publish_failure_does_not_break_upload(client, logged_in, monkeypatch):
    monkeypatch.setattr(Config, 'REDIS_URL', 'redis://fake:6379')
    monkeypatch.setattr(notifications, '_get_client', lambda: FakeRedis(fail=True))
    resp = _upload(client, _png_bytes('gold'))
    assert 'Successful'.encode() in resp.data
    assert len(get_all_media_paths()) == 1


def test_memory_url_is_noop(client, logged_in, monkeypatch):
    # conftest sets REDIS_URL=memory:// — publish_event must not try to
    # build a real client for it.
    monkeypatch.setattr(notifications, '_get_client',
                        lambda: pytest.fail("client built for memory://"))
    resp = _upload(client, _png_bytes('pink'))
    assert 'Successful'.encode() in resp.data


def test_video_event_is_deferred_to_thumb_worker(client, logged_in, fake_redis, monkeypatch):
    jobs = []
    monkeypatch.setattr('app.media.ingest.enqueue_video_thumb',
                        lambda *a, **kw: jobs.append((a, kw)))
    resp = _upload(client, b'not-really-a-video', name='clip.mp4')
    assert 'Successful'.encode() in resp.data
    # Nothing published at ingest time; the event rides the thumb job.
    assert fake_redis.events == []
    assert len(jobs) == 1
    event = jobs[0][1]['notify_event']
    assert event and event['event'] == 'media_added'
    assert event['filepath'].endswith('.mp4')


def test_private_video_job_carries_no_event(client, logged_in, fake_redis, monkeypatch):
    jobs = []
    monkeypatch.setattr('app.media.ingest.enqueue_video_thumb',
                        lambda *a, **kw: jobs.append((a, kw)))
    _upload(client, b'not-really-a-video-2', name='secret.mp4', private=True)
    assert fake_redis.events == []
    assert len(jobs) == 1
    assert jobs[0][1]['notify_event'] is None
