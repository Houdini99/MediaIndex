"""WebDAV endpoint: authentication, throttling, and per-user isolation.

The Flask test client traverses the full wsgi_app stack (ProxyFix →
DispatcherMiddleware → WsgiDAV), so /dav is exercised end-to-end.
"""
import base64
import os

from app.config import Config


def _basic(username, password):
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {'Authorization': f'Basic {cred}'}


def _propfind(client, auth=None, path='/dav/'):
    headers = {'Depth': '1'}
    if auth:
        headers.update(auth)
    return client.open(path, method='PROPFIND', headers=headers)


def _failed_count(ip='127.0.0.1'):
    from app.db_utils import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count FROM users.failed_logins WHERE ip=%s", (ip,))
        row = cur.fetchone()
    return row[0] if row else 0


def test_dav_requires_auth(client):
    assert _propfind(client).status_code == 401


def test_dav_accepts_app_password(client, dav_user):
    uid, username, token = dav_user
    resp = _propfind(client, _basic(username, token))
    assert resp.status_code == 207
    # Successful auth creates the user's staging jail.
    assert os.path.isdir(os.path.join(Config.STAGING_DIR, str(uid)))


def test_dav_rejects_login_password(client, dav_user, user):
    _uid, username, _token = dav_user
    login_password = user[2]
    assert _propfind(client, _basic(username, login_password)).status_code == 401


def test_dav_rejects_user_without_app_password(client, user):
    _uid, username, password = user
    assert _propfind(client, _basic(username, password)).status_code == 401


def test_dav_failures_count_toward_ban(client, dav_user):
    _uid, username, _token = dav_user
    before = _failed_count()
    _propfind(client, _basic(username, 'wrong'))
    _propfind(client, _basic(username, 'wrong'))
    assert _failed_count() == before + 2


def test_dav_banned_ip_rejected(client, dav_user):
    from app.auth.db import ban_ip, unban_ip
    _uid, username, token = dav_user
    ban_ip('127.0.0.1')
    try:
        assert _propfind(client, _basic(username, token)).status_code == 401
    finally:
        unban_ip('127.0.0.1')


def test_dav_put_lands_in_user_staging(client, dav_user):
    uid, username, token = dav_user
    resp = client.put('/dav/test.jpg', data=b'fake-jpeg-bytes',
                      headers=_basic(username, token))
    assert resp.status_code in (201, 204)
    path = os.path.join(Config.STAGING_DIR, str(uid), 'test.jpg')
    assert os.path.isfile(path)
    with open(path, 'rb') as f:
        assert f.read() == b'fake-jpeg-bytes'


def test_dav_propfind_hrefs_carry_mount_prefix(client, dav_user):
    uid, username, token = dav_user
    client.put('/dav/prefixed.jpg', data=b'x', headers=_basic(username, token))
    resp = _propfind(client, _basic(username, token))
    assert b'/dav/prefixed.jpg' in resp.data


def test_dav_users_are_isolated(client, dav_user):
    from app.auth.db import (
        fetch_user_for_login, hash_dav_password, insert_user,
        set_dav_password_hash,
    )
    from werkzeug.security import generate_password_hash

    _uid, username, token = dav_user
    put = client.put('/dav/secret-alice.jpg', data=b'alice-data',
                     headers=_basic(username, token))
    assert put.status_code in (201, 204)

    insert_user('bob', generate_password_hash('bobpass123'))
    bob_id = fetch_user_for_login('bob')[0]
    bob_token = 'bob-dav-token'
    set_dav_password_hash(bob_id, hash_dav_password(bob_token))

    resp = _propfind(client, _basic('bob', bob_token))
    assert resp.status_code == 207
    assert b'secret-alice' not in resp.data
    # Bob can't address Alice's file directly either.
    get = client.get('/dav/secret-alice.jpg', headers=_basic('bob', bob_token))
    assert get.status_code == 404


def test_dav_path_escape_blocked(client, dav_user):
    _uid, username, token = dav_user
    # An encoded '..' that would escape the user jail must not serve
    # another user's (or any outside) path.
    resp = client.get('/dav/..%2F..%2Fdata%2Fusers.db',
                      headers=_basic(username, token))
    assert resp.status_code in (403, 404)


def test_domain_controller_unit(app, dav_user):
    from app.dav.dc import ENVIRON_USER_ID, MediaIndexDomainController
    uid, username, token = dav_user
    dc = MediaIndexDomainController(None, {})
    environ = {'REMOTE_ADDR': '10.0.0.9'}
    assert dc.basic_auth_user('realm', username, token, environ) is True
    assert environ[ENVIRON_USER_ID] == uid
    assert dc.basic_auth_user('realm', username, 'nope', environ) is False
    assert dc.basic_auth_user('realm', 'ghost', token, environ) is False
