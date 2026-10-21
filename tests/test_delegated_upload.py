"""Delegated upload attribution: the Discord bot's 'uploaded_for' field
assigns ownership to the mapped index user — honored only for accounts in
Config.DELEGATED_UPLOADERS (default: 'Bot')."""
import io

from PIL import Image
from werkzeug.security import generate_password_hash

from app.auth.db import insert_user, fetch_user_for_login
from app.media.db import get_all_media_paths, get_media_by_id


def _png_bytes(color):
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buf, format='PNG')
    return buf.getvalue()


def _upload(client, data, uploaded_for=None, name='pic.png'):
    payload = {'files': (io.BytesIO(data), name), 'tags_0': 'discord'}
    if uploaded_for is not None:
        payload['uploaded_for'] = uploaded_for
    return client.post('/upload', data=payload,
                       content_type='multipart/form-data', follow_redirects=True)


def _make_user(username):
    insert_user(username, generate_password_hash('password123'))
    return fetch_user_for_login(username)[0]


def _login(client, username):
    client.post('/logout')
    client.post('/login', data={'username': username, 'password': 'password123'})


def test_delegated_upload_assigns_owner_and_tag(client, user, app):
    _make_user('Bot')
    target = _make_user('bob')
    _login(client, 'Bot')
    resp = _upload(client, _png_bytes('red'), uploaded_for='bob')
    assert 'Successful'.encode() in resp.data
    mid = get_all_media_paths()[0][0]
    m = get_media_by_id(mid)
    assert m['uploaded_by'] == target
    tags = [t.strip() for t in m['tags'].split(',')]
    assert 'user:bob' in tags and 'user:Bot' not in tags


def test_uploaded_for_ignored_for_normal_users(client, logged_in, user):
    """'alice' is not in DELEGATED_UPLOADERS — the field must be ignored."""
    target = _make_user('bob')
    resp = _upload(client, _png_bytes('blue'), uploaded_for='bob')
    assert 'Successful'.encode() in resp.data
    m = get_media_by_id(get_all_media_paths()[0][0])
    assert m['uploaded_by'] == user[0] and m['uploaded_by'] != target
    assert 'user:alice' in m['tags']


def test_unknown_uploaded_for_falls_back_to_uploader(client, app, user):
    bot_uid = _make_user('Bot')
    _login(client, 'Bot')
    resp = _upload(client, _png_bytes('green'), uploaded_for='ghost')
    assert 'Successful'.encode() in resp.data
    m = get_media_by_id(get_all_media_paths()[0][0])
    assert m['uploaded_by'] == bot_uid
    assert 'user:Bot' in m['tags']
