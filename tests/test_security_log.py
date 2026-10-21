"""The security log: one JSON line per sign-in, failed sign-in, ban and
account change, in log/security/security-YYYY-MM-DD.log, kept for good.

What it has to show after an attack: which usernames were tried, whether
they exist, from which address and client, through which door, when the
address was banned, and whether any sign-in succeeded.
"""
import base64
import json
import os
import stat
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

from werkzeug.security import generate_password_hash

from app import security_log
from app.auth.db import (
    fetch_user_for_login, hash_dav_password, insert_user, reset_failed_logins,
    set_dav_password_hash, set_user_admin,
)
from app.config import Config


def _create(username, password='password123', admin=False):
    assert insert_user(username, generate_password_hash(password))
    uid = fetch_user_for_login(username)[0]
    set_user_admin(uid, admin)
    return uid


def _login(client, username, password='password123'):
    return client.post('/login', data={'username': username, 'password': password},
                       headers={'User-Agent': 'test-agent/1.0'})


def _basic(username, password):
    raw = base64.b64encode(f'{username}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


def _events(event=None):
    out = []
    folder = security_log.log_dir()
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        with open(os.path.join(folder, name), encoding='utf-8') as fh:
            out.extend(json.loads(line) for line in fh)
    return [e for e in out if event is None or e['event'] == event]


# ── Failed and successful sign-ins ────────────────────────────────────────
def test_failed_login_names_the_username_and_address(app, client):
    _login(client, 'admin', 'guess')
    [e] = _events()
    assert e['event'] == 'login_failed'
    assert e['user'] == 'admin' and e['known'] is False
    assert e['via'] == 'login' and e['ip'] == '127.0.0.1'
    assert e['ua'] == 'test-agent/1.0'
    assert e['time'].endswith('Z')


def test_failed_login_says_whether_the_account_exists(app, client):
    _create('alice')
    _login(client, 'alice', 'wrong-password')
    assert _events('login_failed')[0]['known'] is True


def test_successful_login_is_logged(app, client):
    _create('alice')
    _login(client, 'alice')
    [e] = _events()
    assert e['event'] == 'login' and e['user'] == 'alice'


def test_the_ban_is_logged_once_when_it_starts(app, client):
    for _ in range(Config.BAN_THRESHOLD):
        _login(client, 'root', 'guess')
    assert len(_events('login_failed')) == Config.BAN_THRESHOLD
    [ban] = _events('ip_banned')
    assert ban['address'] == '127.0.0.1'
    assert ban['failures'] == Config.BAN_THRESHOLD and ban['hours'] == 24
    reset_failed_logins('127.0.0.1')


def test_webdav_and_api_failures_are_logged(app, client):
    uid = _create('alice')
    set_dav_password_hash(uid, hash_dav_password('the-app-password'))
    client.open('/dav/', method='PROPFIND', headers={
        'Depth': '1', 'User-Agent': 'Microsoft-WebDAV-MiniRedir/10.0',
        **_basic('alice', 'wrong')})
    client.post('/api/upload', headers=_basic('mallory', 'wrong'))
    dav, api = _events('login_failed')
    assert (dav['via'], dav['user'], dav['known']) == ('webdav', 'alice', True)
    assert dav['ua'] == 'Microsoft-WebDAV-MiniRedir/10.0'
    assert dav['ip'] == '127.0.0.1'
    assert (api['via'], api['user'], api['known']) == ('api', 'mallory', False)


def test_password_forms_are_logged(app, client):
    _create('alice')
    _login(client, 'alice')
    form = {'current_password': 'wrong', 'new_password': 'my-own-password',
            'confirm_password': 'my-own-password'}
    client.post('/settings/password', data=form)
    client.post('/settings/password', data={**form, 'current_password': 'password123'})
    failed = _events('login_failed')[-1]
    assert (failed['via'], failed['user']) == ('password-change', 'alice')
    [changed] = _events('password_changed')
    assert (changed['user'], changed['via']) == ('alice', 'settings')


# ── Account changes ───────────────────────────────────────────────────────
def test_admin_actions_name_the_admin(app, client):
    _create('root', admin=True)
    _login(client, 'root')
    client.post('/admin/users/create', data={'username': 'bob', 'password': 'password123'})
    bob = fetch_user_for_login('bob')[0]
    client.post(f'/admin/users/{bob}/password', data={'password': 'reset-by-root'})
    client.post(f'/admin/users/{bob}/admin', data={'make_admin': '1'})
    client.post('/admin/bans/ban', data={'ip': '203.0.113.9'})
    client.post('/admin/bans/lift', data={'ip': '203.0.113.9'})
    client.post(f'/admin/users/{bob}/delete', data={'media_action': 'delete'})

    events = [(e['event'], e.get('user'), e.get('address'))
              for e in _events() if e['event'] != 'login']
    assert events == [
        ('user_created', 'bob', None),
        ('password_reset', 'bob', None),
        ('admin_granted', 'bob', None),
        ('ip_banned', None, '203.0.113.9'),
        ('ip_unbanned', None, '203.0.113.9'),
        ('user_deleted', 'bob', None),
    ]
    assert all(e['by'] == 'root' for e in _events() if e['event'] != 'login')
    assert _events('ip_banned')[0]['manual'] is True
    assert _events('user_deleted')[0]['media'] == '0 deleted'


def test_app_passwords_are_logged(app, client):
    _create('alice')
    _login(client, 'alice')
    client.post('/api/dav/password')
    client.post('/api/dav/password/revoke')
    assert [e['event'] for e in _events()][1:] == [
        'app_password_created', 'app_password_revoked']


# ── The file itself ───────────────────────────────────────────────────────
def test_one_file_per_utc_day_readable_by_its_owner_only(app, client):
    _login(client, 'admin', 'guess')
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    path = os.path.join(security_log.log_dir(), f'security-{today}.log')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(security_log.log_dir()).st_mode) == 0o700


def test_a_hostile_username_stays_one_line(app, client):
    name = 'x"}\n{"event": "login", "user": "root' + 'y' * 300
    _login(client, name, 'guess')
    [e] = _events()
    assert e['event'] == 'login_failed'
    assert e['user'].startswith('x"}\n{"event"') and e['user'].endswith('…')
    assert len(e['user']) == 101


def test_rotating_app_log_never_touches_the_security_log(tmp_path):
    """app.log keeps 14 days; the security log lives in its own folder,
    which the rotation's clean-up doesn't look into."""
    (tmp_path / 'security').mkdir()
    for day in range(1, 29):
        (tmp_path / f'app.log.2026-09-{day:02d}').write_text('')
        (tmp_path / 'security' / f'security-2026-09-{day:02d}.log').write_text('')
    handler = TimedRotatingFileHandler(str(tmp_path / 'app.log'), when='midnight',
                                       backupCount=14)
    handler.suffix = '%Y-%m-%d'
    try:
        doomed = handler.getFilesToDelete()
    finally:
        handler.close()
    assert len(doomed) == 14
    assert all(os.path.basename(p).startswith('app.log.') for p in doomed)


def test_a_write_failure_never_blocks_a_login(app, client, tmp_path, monkeypatch):
    blocker = tmp_path / 'not-a-folder'
    blocker.write_text('')
    monkeypatch.setattr(Config, 'LOG_FOLDER', str(blocker))
    _create('alice')
    assert _login(client, 'alice').status_code == 302


# ── Admin → Logs ──────────────────────────────────────────────────────────
def test_admin_log_viewer_shows_the_security_log(app, client):
    _create('root', admin=True)
    _login(client, 'root')
    _login(app.test_client(), 'mallory', 'guess')
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    html = client.get(f'/admin/logs?file=security/security-{today}.log').data
    assert b'Security log' in html
    assert b'mallory' in html


def test_admin_log_viewer_refuses_other_paths(app, client):
    _create('root', admin=True)
    _login(client, 'root')
    html = client.get('/admin/logs?file=security/../../../etc/passwd').data
    assert b'root:x:' not in html
