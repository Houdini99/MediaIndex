"""Access-control guarantees for private media.

1. The file-serving gate fails CLOSED — a file present on disk with no media
   row (the shape of a transient encrypt/decrypt artifact, or an orphan) is
   never served.
2. The feed (/api/feed) never surfaces another user's private media, while a
   user still sees their own private uploads.
"""
import io
import os

from PIL import Image
from werkzeug.security import generate_password_hash

from app.config import Config
from app.auth.db import insert_user
from app.media.db import get_all_media_paths


def _png(color):
    buf = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(buf, format='PNG')
    return buf.getvalue()


def _upload(client, color, name, private=False):
    data = {'files': (io.BytesIO(_png(color)), name), 'tags_0': 'feedtest'}
    if private:
        data['private_0'] = '1'
    resp = client.post('/upload', data=data,
                       content_type='multipart/form-data', follow_redirects=True)
    assert 'Successful'.encode() in resp.data


def _login(client, username, password='password123'):
    client.post('/logout')
    resp = client.post('/login', data={'username': username, 'password': password})
    assert resp.status_code == 302


def _clear_feed_cache():
    """The feed's shuffled-id cache is a module global that survives across
    tests; clear it so a stale entry can't mask (or fake) a result."""
    from app.media import routes
    with routes._feed_cache_lock:
        routes._feed_cache.clear()


def _feed_ids(client, seed=12345):
    """Every media id the feed shows this session, across all pages."""
    ids, page = [], 1
    while True:
        payload = client.get(f'/api/feed?seed={seed}&page={page}').get_json()
        ids += [m['id'] for m in payload['media']]
        if not payload.get('has_more'):
            break
        page += 1
    return ids


# ── 1. Serving gate fails closed ───────────────────────────────────────────
def test_serving_gate_fails_closed_on_unindexed_file(client, logged_in):
    """A file sitting in the media/thumb folders with no media row must not be
    served. This is what keeps a plaintext file left behind mid-encryption
    from being fetched just because no row currently claims its name."""
    stray = 'deadbeef.png'
    with open(os.path.join(Config.UPLOAD_FOLDER, stray), 'wb') as f:
        f.write(_png('orange'))
    with open(os.path.join(Config.THUMB_FOLDER, stray), 'wb') as f:
        f.write(_png('orange'))
    assert get_all_media_paths() == []          # no row references either file
    assert client.get(f'/media/{stray}').status_code == 404
    assert client.get(f'/thumbs/{stray}').status_code == 404


# ── 2. Feed never leaks another user's private media ───────────────────────
def test_feed_hides_other_users_private_media(client, logged_in):
    _clear_feed_cache()
    # alice (the logged_in fixture user) uploads one public + one private item.
    _upload(client, 'red', 'alice_public.png')
    _upload(client, 'blue', 'alice_private.png', private=True)
    priv_id = next(mid for mid, fp, _ in get_all_media_paths() if fp.endswith('.enc'))
    pub_id = next(mid for mid, fp, _ in get_all_media_paths() if not fp.endswith('.enc'))

    # bob is a second, unrelated user.
    insert_user('bob', generate_password_hash('password123'))
    _login(client, 'bob')

    bob_ids = _feed_ids(client)
    assert priv_id not in bob_ids               # alice's PRIVATE item is hidden
    assert pub_id in bob_ids                     # alice's PUBLIC item is visible
    first = client.get('/api/feed?seed=12345&page=1').get_json()
    assert all(not m['is_private'] for m in first['media'])
    # the private '.enc' filename must never appear in the serialized payload.
    assert all('.enc' not in (m.get('filepath') or '') for m in first['media'])

    # alice herself still sees her own private item in her own feed.
    _clear_feed_cache()
    _login(client, 'alice')
    assert priv_id in _feed_ids(client)
