"""HTTP Range handling for encrypted media, and private-media access control."""
import io

import pytest
from PIL import Image

from app.media.db import get_all_media_paths


@pytest.fixture()
def private_upload(client, logged_in):
    """Upload a private image; return (fname, plaintext_bytes)."""
    buf = io.BytesIO()
    Image.new('RGB', (64, 64), 'purple').save(buf, format='PNG')
    data = buf.getvalue()
    resp = client.post(
        '/upload',
        data={'files': (io.BytesIO(data), 'secret.png'), 'tags_0': 't', 'private_0': '1'},
        content_type='multipart/form-data', follow_redirects=True,
    )
    assert 'Successful'.encode() in resp.data
    fname = get_all_media_paths()[0][1]
    assert fname.endswith('.enc')
    return fname, data


def test_full_request_decrypts(client, private_upload):
    fname, data = private_upload
    resp = client.get(f'/media/{fname}')
    assert resp.status_code == 200
    assert resp.data == data
    assert resp.headers['Content-Length'] == str(len(data))
    assert resp.headers['Accept-Ranges'] == 'bytes'
    assert resp.headers['Content-Type'] == 'image/png'


def test_range_slice(client, private_upload):
    fname, data = private_upload
    resp = client.get(f'/media/{fname}', headers={'Range': 'bytes=10-49'})
    assert resp.status_code == 206
    assert resp.data == data[10:50]
    assert resp.headers['Content-Range'] == f'bytes 10-49/{len(data)}'
    assert resp.headers['Content-Length'] == '40'


def test_range_open_ended(client, private_upload):
    fname, data = private_upload
    resp = client.get(f'/media/{fname}', headers={'Range': 'bytes=100-'})
    assert resp.status_code == 206
    assert resp.data == data[100:]


def test_range_out_of_bounds_416(client, private_upload):
    fname, data = private_upload
    resp = client.get(f'/media/{fname}', headers={'Range': f'bytes=0-{len(data)}'})
    assert resp.status_code == 416
    assert resp.headers['Content-Range'] == f'bytes */{len(data)}'


def test_range_inverted_416(client, private_upload):
    fname, _ = private_upload
    resp = client.get(f'/media/{fname}', headers={'Range': 'bytes=50-10'})
    assert resp.status_code == 416


def test_range_garbage_416(client, private_upload):
    fname, _ = private_upload
    resp = client.get(f'/media/{fname}', headers={'Range': 'bytes=abc-def'})
    assert resp.status_code == 416


def test_private_media_hidden_from_other_users(client, private_upload, app):
    from app.auth.db import insert_user
    from werkzeug.security import generate_password_hash
    fname, _ = private_upload
    insert_user('bob', generate_password_hash('password123'))
    client.post('/logout')
    client.post('/login', data={'username': 'bob', 'password': 'password123'})
    assert client.get(f'/media/{fname}').status_code == 404
    # ...and it doesn't appear in bob's gallery API
    listing = client.get('/api/media').get_json()
    assert listing['media'] == []


def test_media_requires_login(client, private_upload):
    fname, _ = private_upload
    client.post('/logout')
    resp = client.get(f'/media/{fname}')
    assert resp.status_code == 302  # redirected to login
