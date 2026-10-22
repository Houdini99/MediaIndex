"""Users choosing their own password.

Accounts an admin creates start with a password the admin chose, and so
does an account whose password an admin resets. Their next login leads to
/login/new-password and nowhere else until the user picks their own. Any user can change their password on /settings. Both
need the current password, end every other session, and leave the app
password alone.
"""
import base64

from werkzeug.security import generate_password_hash

from app.auth.db import (
    _conn, fetch_user_by_id, fetch_user_for_login, hash_dav_password,
    insert_user, set_dav_password_hash, set_user_admin,
)

NEW = 'my-own-password'


def _create(username, password='password123', admin=False, must_change=False):
    assert insert_user(username, generate_password_hash(password),
                       must_change_password=must_change)
    uid = fetch_user_for_login(username)[0]
    set_user_admin(uid, admin)
    return uid


def _login(client, username, password='password123'):
    resp = client.post('/login', data={'username': username, 'password': password})
    assert resp.status_code == 302
    return resp


def _form(current='password123', new=NEW, confirm=None):
    return {'current_password': current, 'new_password': new,
            'confirm_password': new if confirm is None else confirm}


def _must_change(uid):
    return fetch_user_by_id(uid)[4]


def _fail_count():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count FROM users.failed_logins WHERE ip='127.0.0.1'")
        row = cur.fetchone()
        return row[0] if row else 0


# ── Accounts an admin creates ─────────────────────────────────────────────
def test_admin_created_account_must_change_its_password(app, client):
    _create('root', admin=True)
    _login(client, 'root')
    client.post('/admin/users/create',
                data={'username': 'bob', 'password': 'password123'})
    assert _must_change(fetch_user_for_login('bob')[0]) is True


def test_admin_reset_makes_the_user_choose_again(app, client):
    _create('root', admin=True)
    _login(client, 'root')
    bob = _create('bob')
    client.post(f'/admin/users/{bob}/password', data={'password': 'reset-by-admin'})
    assert _must_change(bob) is True

    browser = app.test_client()
    _login(browser, 'bob', 'reset-by-admin')
    assert browser.get('/').headers['Location'].endswith('/login/new-password')
    browser.post('/login/new-password', data=_form(current='reset-by-admin'))
    assert _must_change(bob) is False
    assert browser.get('/').status_code == 200


def test_admin_resetting_their_own_password_is_not_asked(app, client):
    root = _create('root', admin=True)
    _login(client, 'root')
    client.post(f'/admin/users/{root}/password', data={'password': 'my-new-password'})
    assert _must_change(root) is False
    assert client.get('/').status_code == 200


def test_cli_reset_leaves_the_flag_alone(app):
    """set_password.py and set_password_hash.py neither set the flag nor
    let a user skip choosing their own password."""
    from app.auth.db import update_user_password
    alice = _create('alice')
    bob = _create('bob', must_change=True)
    for uid in (alice, bob):
        update_user_password(uid, generate_password_hash('set-from-the-cli'))
    assert _must_change(alice) is False
    assert _must_change(bob) is True


def test_set_password_hash_only_stores_what_logins_can_check(monkeypatch):
    """A hash Werkzeug cannot check (its old 'plain' scheme) would turn
    every login of that user into a 500, so the script must refuse it."""
    import os
    monkeypatch.syspath_prepend(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scripts'))
    from set_password_hash import looks_like_hash
    for method in ('scrypt', 'pbkdf2:sha256'):
        assert looks_like_hash(generate_password_hash('pw', method))[0]
    assert not looks_like_hash('plain$$pw')[0]
    assert not looks_like_hash('scrypt:32768:8:1$$deadbeef')[0]   # salt lost


def test_registered_accounts_are_not_asked(app, client, monkeypatch):
    """The first admin registers with a password they chose themselves."""
    from app.config import Config
    monkeypatch.setattr(Config, 'REGISTRATION_OPEN', True)
    client.post('/register', data={'username': 'root', 'password': 'password123'})
    assert _must_change(fetch_user_for_login('root')[0]) is False


def test_first_login_leads_to_the_password_form(app, client):
    _create('bob', must_change=True)
    resp = _login(client, 'bob')
    resp = client.get(resp.headers['Location'])
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/login/new-password')
    assert b'Choose your password' in client.get('/login/new-password').data


def test_every_page_leads_to_the_form_until_changed(app, client):
    _create('bob', must_change=True)
    _login(client, 'bob')
    for path in ('/', '/feed', '/settings', '/api/media', '/upload', '/login'):
        resp = client.get(path)
        assert resp.status_code == 302, path
        assert resp.headers['Location'].endswith('/login/new-password'), path
    # Uploads are refused too, not just pages.
    resp = client.post('/upload', data={})
    assert resp.headers['Location'].endswith('/login/new-password')


def test_logout_and_static_files_still_work(app, client):
    _create('bob', must_change=True)
    _login(client, 'bob')
    assert client.get('/static/css/auth.css').status_code == 200
    resp = client.post('/logout')
    assert resp.headers['Location'].endswith('/login')
    assert client.get('/login').status_code == 200


def test_changing_it_opens_the_index(app, client):
    uid = _create('bob', must_change=True)
    _login(client, 'bob')
    resp = client.post('/login/new-password', data=_form())
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/')
    assert _must_change(uid) is False
    assert client.get('/').status_code == 200
    # The new password logs in; the admin's no longer does.
    _login(app.test_client(), 'bob', NEW)
    resp = app.test_client().post('/login', data={'username': 'bob',
                                                  'password': 'password123'})
    assert resp.status_code == 200 and b'Invalid login' in resp.data


def test_form_refuses_bad_input(app, client):
    uid = _create('bob', must_change=True)
    _login(client, 'bob')
    cases = [
        (_form(current='wrong-password'), b'current password is wrong'),
        (_form(new='short'), b'at least 8 characters'),
        (_form(confirm='something-else'), b'match'),
        # Keeping the admin's password would defeat the point.
        (_form(new='password123'), b'different from the current one'),
    ]
    for data, message in cases:
        resp = client.post('/login/new-password', data=data, follow_redirects=True)
        assert message in resp.data, message
        assert _must_change(uid) is True
    assert client.get('/').headers['Location'].endswith('/login/new-password')


def test_form_is_only_for_accounts_that_must_change(app, client):
    _create('alice')
    _login(client, 'alice')
    resp = client.get('/login/new-password')
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/')


def test_app_password_works_before_the_change(app, client):
    """The bots' account is created by an admin too; its app password must
    not depend on a browser login ever changing the password."""
    uid = _create('Bot', must_change=True)
    set_dav_password_hash(uid, hash_dav_password('app-token-123'))
    auth = base64.b64encode(b'Bot:app-token-123').decode()
    resp = client.get('/api/site-logins',
                      headers={'Authorization': f'Basic {auth}'})
    assert resp.status_code == 200


# ── Changing it on /settings ──────────────────────────────────────────────
def test_settings_change_keeps_this_browser_and_ends_the_others(app, client):
    uid = _create('alice')
    other = app.test_client()
    _login(other, 'alice')
    _login(client, 'alice')

    resp = client.post('/settings/password', data=_form(), follow_redirects=True)
    assert b'Password changed' in resp.data

    assert client.get('/settings').status_code == 200
    resp = other.get('/')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']
    _login(app.test_client(), 'alice', NEW)
    assert _must_change(uid) is False


def test_settings_change_needs_the_current_password(app, client):
    _create('alice')
    _login(client, 'alice')
    before = _fail_count()
    resp = client.post('/settings/password', data=_form(current='wrong-password'),
                       follow_redirects=True)
    assert b'current password is wrong' in resp.data
    # Guessing here counts toward the IP ban, like a failed login.
    assert _fail_count() == before + 1
    _login(app.test_client(), 'alice')  # the old password still works


def test_settings_page_shows_the_form(app, client):
    _create('alice')
    _login(client, 'alice')
    html = client.get('/settings').data
    assert b'action="/settings/password"' in html
    assert b'name="current_password"' in html


def test_settings_change_needs_a_login(app, client):
    resp = client.post('/settings/password', data=_form())
    assert resp.status_code == 302 and '/login' in resp.headers['Location']
