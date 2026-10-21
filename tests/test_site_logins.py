"""app/site_logins.py, its admin page and its bot API.

The stored logins are live account sessions, so most of what is pinned
here is about who gets them and what they are allowed to become:

  * a cookies.txt filed under a site keeps only that site's cookies;
  * what the database holds is ciphertext under SECRET_KEY;
  * only an admin edits them, and only DELEGATED_UPLOADERS accounts (the
    bots) read them, one site at a time;
  * a bot's write-back of rotated cookies can never undo an admin's upload
    or removal, and never logs a site out.
"""
import base64
import io

import pytest
from werkzeug.security import generate_password_hash

from app import site_logins as sl
from app.auth.db import (
    fetch_user_for_login,
    hash_dav_password,
    insert_user,
    set_dav_password_hash,
    set_user_admin,
)
from app.config import Config
from app.db_utils import db_conn

TOKEN = 'app-password-token-123'

YOUTUBE_COOKIES = (
    '# Netscape HTTP Cookie File\n'
    '# a comment\n'
    '.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tsecret-sapisid\n'
    '#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tLOGIN_INFO\tli\n'
    '.google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tgoogle-sid\n'
    '.bank.example\tTRUE\t/\tTRUE\t1893456000\tsession\tbank-session\n'
)
LOGGED_OUT = (
    '# Netscape HTTP Cookie File\n'
    '.youtube.com\tTRUE\t/\tTRUE\t1893456000\tPREF\tf6=40000000\n'
)


def _basic(username, password=TOKEN):
    raw = base64.b64encode(f'{username}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


def _account(username, admin=False, with_token=True):
    insert_user(username, generate_password_hash('password123'))
    uid = fetch_user_for_login(username)[0]
    set_user_admin(uid, admin)
    if with_token:
        set_dav_password_hash(uid, hash_dav_password(TOKEN))
    return uid


@pytest.fixture()
def admin_client(client):
    _account('root', admin=True, with_token=False)
    resp = client.post('/login', data={'username': 'root',
                                       'password': 'password123'})
    assert resp.status_code == 302
    return client


@pytest.fixture()
def bot_account(app):
    return _account('Bot')


def _store(platform_id, text):
    cleaned, _kept, _dropped = sl.clean(sl.platform(platform_id), text)
    return sl.save_upload(platform_id, cleaned, None)


# ── Parsing and filtering ─────────────────────────────────────────────────
def test_httponly_lines_are_cookies_and_comments_are_not():
    names = [c.name for c in sl.parse(YOUTUBE_COOKIES)]
    assert names == ['SAPISID', 'LOGIN_INFO', 'SID', 'session']


def test_a_malformed_line_is_skipped_not_fatal():
    text = YOUTUBE_COOKIES + 'not\ta\tcookie\n.youtube.com\tTRUE\t/\tx\tbad\tA\tB\n'
    assert len(sl.parse(text)) == 4


def test_an_upload_keeps_only_its_own_sites_cookies():
    """A whole-browser export filed under YouTube must not hand the bots
    the bank session that came along with it."""
    text, kept, dropped = sl.clean(sl.platform('youtube'), YOUTUBE_COOKIES)
    assert (kept, dropped) == (2, 2)
    assert 'secret-sapisid' in text and 'LOGIN_INFO' in text
    assert 'bank-session' not in text and 'google-sid' not in text
    assert text.startswith(sl.HEADER + '\n')


def test_other_sites_keeps_everything():
    _text, kept, dropped = sl.clean(sl.OTHER, YOUTUBE_COOKIES)
    assert (kept, dropped) == (4, 0)


@pytest.mark.parametrize('text, says', [
    ('{"cookies": []}', 'Netscape'),
    ('', 'Netscape'),
    ('.bank.example\tTRUE\t/\tTRUE\t0\tsession\tx\n', 'belong to YouTube'),
])
def test_unusable_uploads_are_refused_with_a_reason(text, says):
    with pytest.raises(ValueError, match=says):
        sl.clean(sl.platform('youtube'), text)


def test_an_oversized_upload_is_refused():
    line = '.youtube.com\tTRUE\t/\tTRUE\t0\tA\t' + 'x' * 1000 + '\n'
    with pytest.raises(ValueError, match='over'):
        sl.clean(sl.platform('youtube'), line * 1100)


@pytest.mark.parametrize('url, platform_id', [
    ('https://www.youtube.com/watch?v=1', 'youtube'),
    ('https://youtu.be/1', 'youtube'),
    ('https://m.youtube.com/shorts/1', 'youtube'),
    ('https://x.com/u/status/1', 'twitter'),
    ('https://www.instagram.com/reel/1/', 'instagram'),
    ('https://fb.watch/abc/', 'facebook'),
    ('https://old.reddit.com/r/a/', 'reddit'),
    ('https://notyoutube.com/watch', None),
    ('https://x.com.evil.net/u', None),
    ('https://example.com/video', None),
    ('http://[::1/broken', None),
])
def test_links_map_to_their_platform_on_label_boundaries(url, platform_id):
    plat = sl.platform_for_url(url)
    assert (plat.id if plat else None) == platform_id


def test_a_login_cookie_only_counts_on_the_sites_own_domain():
    """A Google login sets SAPISID on .google.com first; yt-dlp can't use
    that for YouTube, so it must not read as logged in."""
    yt = sl.platform('youtube')
    google_only = '.google.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tx\n'
    assert sl.summary(yt, google_only)['logged_in'] is False
    assert sl.summary(yt, YOUTUBE_COOKIES)['logged_in'] is True
    assert sl.summary(yt, YOUTUBE_COOKIES)['expires'] == 1893456000


def test_an_expired_login_is_reported_as_such():
    yt = sl.platform('youtube')
    stale = '.youtube.com\tTRUE\t/\tTRUE\t1000000000\tSAPISID\tx\n'
    assert sl.summary(yt, stale)['expired'] is True


# ── Storage ───────────────────────────────────────────────────────────────
def test_the_database_holds_ciphertext_only(app):
    _store('youtube', YOUTUBE_COOKIES)
    with db_conn() as conn:
        blob = bytes(conn.execute(
            "SELECT cookies FROM users.site_logins").fetchone()[0])
    assert b'secret-sapisid' not in blob
    assert 'secret-sapisid' in sl.get_login('youtube').text


def test_a_changed_secret_key_makes_a_login_unreadable(app, monkeypatch):
    _store('youtube', YOUTUBE_COOKIES)
    monkeypatch.setattr(Config, 'SECRET_KEY', 'a-different-secret')
    login = sl.get_login('youtube')
    assert login is not None and not login.readable
    assert sl.login_for_url('https://youtu.be/1') is None


def test_a_ciphertext_moved_to_another_site_does_not_open(app):
    _store('youtube', YOUTUBE_COOKIES)
    with db_conn() as conn:
        conn.execute("UPDATE users.site_logins SET platform = 'reddit'")
    assert not sl.get_login('reddit').readable


def test_a_link_gets_its_sites_login_else_the_other_fallback(app):
    _store('other', YOUTUBE_COOKIES)
    assert sl.login_for_url('https://youtu.be/1').platform.id == 'other'
    _store('youtube', YOUTUBE_COOKIES)
    assert sl.login_for_url('https://youtu.be/1').platform.id == 'youtube'
    assert sl.login_for_url('https://vimeo.com/1').platform.id == 'other'


def test_revisions_never_repeat_across_a_delete(app):
    """A write-back from before a removal must not match a new upload."""
    first = _store('youtube', YOUTUBE_COOKIES)
    sl.delete_login('youtube')
    second = _store('youtube', YOUTUBE_COOKIES)
    assert second != first
    assert sl.save_rotation('youtube', first, YOUTUBE_COOKIES) is None


# ── Admin page ────────────────────────────────────────────────────────────
def test_the_page_is_admin_only(client, logged_in):
    assert client.get('/admin/site-logins').status_code == 403
    resp = client.post('/admin/site-logins/youtube/upload',
                       data={'cookies': (io.BytesIO(b'x'), 'cookies.txt')},
                       content_type='multipart/form-data')
    assert resp.status_code == 403
    assert sl.get_login('youtube') is None


def test_an_admin_uploads_and_sees_the_status(admin_client):
    resp = admin_client.post(
        '/admin/site-logins/youtube/upload',
        data={'cookies': (io.BytesIO(YOUTUBE_COOKIES.encode()), 'c.txt')},
        content_type='multipart/form-data', follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert 'Saved the YouTube login: 2 cookie(s)' in page
    assert '2 for other sites were left out' in page
    assert 'logged in' in page
    assert 'secret-sapisid' not in page        # never echoed back
    login = sl.get_login('youtube')
    assert 'bank-session' not in login.text
    assert login.uploaded_by == 'root'


def test_an_upload_without_the_login_cookie_warns(admin_client):
    resp = admin_client.post(
        '/admin/site-logins/youtube/upload',
        data={'cookies': (io.BytesIO(LOGGED_OUT.encode()), 'c.txt')},
        content_type='multipart/form-data', follow_redirects=True)
    assert 'still run logged out' in resp.get_data(as_text=True)


def test_a_bad_upload_stores_nothing(admin_client):
    resp = admin_client.post(
        '/admin/site-logins/youtube/upload',
        data={'cookies': (io.BytesIO(b'{"json": true}'), 'c.json')},
        content_type='multipart/form-data', follow_redirects=True)
    assert 'Netscape' in resp.get_data(as_text=True)
    assert sl.get_login('youtube') is None


def test_an_unknown_site_is_refused(admin_client):
    resp = admin_client.post(
        '/admin/site-logins/myspace/upload',
        data={'cookies': (io.BytesIO(YOUTUBE_COOKIES.encode()), 'c.txt')},
        content_type='multipart/form-data', follow_redirects=True)
    assert 'Unknown site' in resp.get_data(as_text=True)


def test_an_admin_removes_a_login(admin_client):
    _store('youtube', YOUTUBE_COOKIES)
    resp = admin_client.post('/admin/site-logins/youtube/remove',
                             follow_redirects=True)
    assert 'Removed the YouTube login' in resp.get_data(as_text=True)
    assert sl.get_login('youtube') is None


# ── Bot API ───────────────────────────────────────────────────────────────
def _cookies(client, url, as_user='Bot', password=TOKEN):
    return client.get('/api/site-logins/cookies', query_string={'url': url},
                      headers=_basic(as_user, password))


def _rotate(client, platform_id, revision, text, as_user='Bot'):
    return client.put(f'/api/site-logins/{platform_id}/cookies',
                      json={'revision': revision, 'cookies': text},
                      headers=_basic(as_user))


def test_the_api_needs_an_app_password(client, bot_account):
    _store('youtube', YOUTUBE_COOKIES)
    assert client.get('/api/site-logins/cookies',
                      query_string={'url': 'https://youtu.be/1'}
                      ).status_code == 401
    assert _cookies(client, 'https://youtu.be/1',
                    password='wrong').status_code == 401


def test_only_delegated_accounts_may_read_logins(client, bot_account):
    """An ordinary WebDAV app password must not be a key to the admin's
    YouTube account."""
    _account('mallory')
    _store('youtube', YOUTUBE_COOKIES)
    assert _cookies(client, 'https://youtu.be/1',
                    as_user='mallory').status_code == 403
    assert client.get('/api/site-logins',
                      headers=_basic('mallory')).status_code == 403
    assert _rotate(client, 'youtube', 1, YOUTUBE_COOKIES,
                   as_user='mallory').status_code == 403


def test_a_bot_gets_the_login_for_a_link(client, bot_account):
    revision = _store('youtube', YOUTUBE_COOKIES)
    resp = _cookies(client, 'https://www.youtube.com/watch?v=1')
    data = resp.get_json()
    assert resp.status_code == 200
    assert 'no-store' in resp.headers['Cache-Control']
    assert data['platform'] == 'youtube' and data['revision'] == revision
    assert 'secret-sapisid' in data['cookies']


def test_no_login_for_the_site_is_a_404(client, bot_account):
    _store('youtube', YOUTUBE_COOKIES)
    assert _cookies(client, 'https://vimeo.com/1').status_code == 404


def test_the_list_names_sites_but_carries_no_cookies(client, bot_account):
    _store('youtube', YOUTUBE_COOKIES)
    _store('instagram',
           '.instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tx\n')
    resp = client.get('/api/site-logins', headers=_basic('Bot'))
    logins = {e['platform']: e for e in resp.get_json()['logins']}
    assert logins['youtube']['logged_in'] is True
    assert logins['instagram']['logged_in'] is False
    assert 'secret-sapisid' not in resp.get_data(as_text=True)


def test_a_rotation_is_saved_and_bumps_the_revision(client, bot_account):
    revision = _store('youtube', YOUTUBE_COOKIES)
    rotated = YOUTUBE_COOKIES.replace('secret-sapisid', 'rotated-sapisid')
    resp = _rotate(client, 'youtube', revision, rotated)
    assert resp.status_code == 200
    assert resp.get_json()['revision'] != revision
    login = sl.get_login('youtube')
    assert 'rotated-sapisid' in login.text
    assert 'bank-session' not in login.text      # filtered like an upload
    assert login.rotated_at is not None


def test_a_rotation_never_overwrites_a_newer_upload(client, bot_account):
    old = _store('youtube', YOUTUBE_COOKIES)
    _store('youtube', YOUTUBE_COOKIES.replace('secret-sapisid', 'fresh'))
    resp = _rotate(client, 'youtube', old,
                   YOUTUBE_COOKIES.replace('secret-sapisid', 'stale'))
    assert resp.status_code == 409
    assert 'fresh' in sl.get_login('youtube').text


def test_a_rotation_cannot_bring_back_a_removed_login(client, bot_account):
    revision = _store('youtube', YOUTUBE_COOKIES)
    sl.delete_login('youtube')
    assert _rotate(client, 'youtube', revision,
                   YOUTUBE_COOKIES).status_code == 409
    assert sl.get_login('youtube') is None


def test_a_rotation_that_would_log_the_site_out_is_refused(client,
                                                           bot_account):
    """A public video downloads fine logged out; keeping that jar would
    throw the session away."""
    revision = _store('youtube', YOUTUBE_COOKIES)
    assert _rotate(client, 'youtube', revision,
                   LOGGED_OUT).status_code == 422
    assert 'secret-sapisid' in sl.get_login('youtube').text


def test_an_unchanged_rotation_writes_nothing(client, bot_account):
    revision = _store('youtube', YOUTUBE_COOKIES)
    # Only the foreign cookies differ, and those are never kept.
    resp = _rotate(client, 'youtube', revision,
                   YOUTUBE_COOKIES.replace('bank-session', 'other'))
    assert resp.get_json()['revision'] == revision


@pytest.mark.parametrize('body', [
    {'revision': '1', 'cookies': YOUTUBE_COOKIES},
    {'revision': True, 'cookies': YOUTUBE_COOKIES},
    {'revision': 1},
    {'revision': 1, 'cookies': 'not a cookies file'},
])
def test_a_malformed_rotation_is_a_400(client, bot_account, body):
    _store('youtube', YOUTUBE_COOKIES)
    resp = client.put('/api/site-logins/youtube/cookies', json=body,
                      headers=_basic('Bot'))
    assert resp.status_code == 400


def test_a_rotation_for_an_unknown_site_is_a_404(client, bot_account):
    assert _rotate(client, 'myspace', 1, YOUTUBE_COOKIES).status_code == 404
