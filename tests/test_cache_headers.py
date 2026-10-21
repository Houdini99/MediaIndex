"""Response caching policy.

Pages must never be cached; media and thumbnails must be cached hard.

The blanket `no-store` this replaces was applied to *every* response,
including the content-addressed media routes — so 87 MB of thumbnails were
re-fetched on every navigation and every video scrub re-downloaded ranges
the browser already had. The names are `<sha256>.<ext>`,
so a URL can only ever return the same bytes; the only reason not to cache
them was an over-broad header.
"""
import io

from PIL import Image

from app.auth.db import insert_user
from app import IMMUTABLE_CACHE
from werkzeug.security import generate_password_hash


def _png(color):
    buf = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(buf, format='PNG')
    return buf.getvalue()


def _user(client, name='cacher'):
    insert_user(name, generate_password_hash('password123'))
    client.post('/login', data={'username': name, 'password': 'password123'})


def _upload(client, name, private=False):
    data = {'files': (io.BytesIO(_png('red')), name), 'tags_0': 'cachetest'}
    if private:
        data['private_0'] = '1'
    resp = client.post('/upload', data=data,
                       content_type='multipart/form-data', follow_redirects=True)
    assert 'Successful'.encode() in resp.data


def _stored_names(client):
    """(media filename, thumb filename) of the single uploaded row."""
    payload = client.get('/api/feed?seed=1').get_json()
    item = payload['media'][0]
    return item['filepath'], item['thumb']


# ── Pages stay uncacheable ─────────────────────────────────────────────────
def test_pages_are_never_cached(client):
    for path in ('/login', '/'):
        resp = client.get(path, follow_redirects=True)
        cc = resp.headers['Cache-Control']
        assert 'no-store' in cc, path
        assert 'immutable' not in cc, path


def test_json_apis_are_never_cached(app, client):
    _user(client)
    resp = client.get('/api/tags')
    assert 'no-store' in resp.headers['Cache-Control']


# ── Media and thumbnails are cached hard ───────────────────────────────────
def test_media_is_cached_immutably(app, client):
    _user(client)
    _upload(client, 'cache.png')
    fname, thumb = _stored_names(client)

    for path in (f'/media/{fname}', f'/thumbs/{thumb}'):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers['Cache-Control'] == IMMUTABLE_CACHE, path
        # HTTP/1.0 Pragma would defeat the whole point on an older cache.
        assert 'Pragma' not in resp.headers, path


def test_cached_media_stays_private_to_one_browser(app, client):
    """`private` is required: these same routes serve other users' private
    media to their owner, so a shared cache must never keep a copy."""
    _user(client)
    _upload(client, 'cache2.png')
    fname, _ = _stored_names(client)
    assert 'private' in client.get(f'/media/{fname}').headers['Cache-Control']


def test_private_media_is_also_cacheable_for_its_owner(app, client):
    """An encrypted file is just as immutable as a public one — the name
    carries a .enc suffix, so the URL still identifies fixed bytes."""
    _user(client)
    _upload(client, 'secret.png', private=True)
    fname, _ = _stored_names(client)
    assert fname.endswith('.enc')
    resp = client.get(f'/media/{fname}')
    assert resp.status_code == 200
    assert resp.headers['Cache-Control'] == IMMUTABLE_CACHE


def test_range_requests_are_cacheable(app, client):
    """206 is the common case for video scrubbing — the whole point of
    caching media is that a re-seek does not re-download."""
    _user(client)
    _upload(client, 'ranged.png', private=True)
    fname, _ = _stored_names(client)
    resp = client.get(f'/media/{fname}', headers={'Range': 'bytes=0-5'})
    assert resp.status_code == 206
    assert resp.headers['Cache-Control'] == IMMUTABLE_CACHE


# ── Errors and mutable content must not be cached ──────────────────────────
def test_missing_media_is_not_cached(app, client):
    """A 404 is a transient condition; caching it for a year would hide the
    file once it exists."""
    _user(client)
    resp = client.get('/media/deadbeef.png')
    assert resp.status_code == 404
    assert 'no-store' in resp.headers['Cache-Control']


def test_staging_files_are_not_cached(app, client):
    """Staging endpoints are keyed by row id, not content hash — a WebDAV
    client overwriting a queued file reuses the same id, so a cached copy
    would be wrong."""
    from app import IMMUTABLE_ENDPOINTS
    assert 'media.staging_file' not in IMMUTABLE_ENDPOINTS
    assert 'media.staging_thumb' not in IMMUTABLE_ENDPOINTS


# ── CSP ────────────────────────────────────────────────────────────────────
def test_csp_no_longer_allows_inline_script(client):
    """Dropping 'unsafe-inline' is only meaningful while no inline handler
    or inline <script> remains; tests/test_no_inline_js.py guards that."""
    csp = client.get('/login').headers['Content-Security-Policy']
    assert "script-src 'self';" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
