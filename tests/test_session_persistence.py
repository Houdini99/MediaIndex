"""Login survives a browser/PC restart.

Before this, login() set session['user_id'] on a non-permanent session, so
Flask emitted a bare session cookie: no Max-Age, discarded by the browser on
close, and — because Flask only enforces the signed timestamp for permanent
sessions — valid forever on the server side. Both halves are
pinned here: the cookie must now carry an expiry, and that expiry must be the
configured window rather than unbounded.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from werkzeug.security import generate_password_hash


def _create(username='alice', password='password123'):
    from app.auth.db import insert_user, fetch_user_for_login
    assert insert_user(username, generate_password_hash(password))
    return fetch_user_for_login(username)[0]


def _login(client, username='alice', password='password123'):
    return client.post('/login', data={'username': username,
                                       'password': password})


def test_login_cookie_outlives_the_browser(app, client):
    """A session cookie with no expiry dies on browser restart — this one has one."""
    _create()
    resp = _login(client)

    cookies = [h for k, h in resp.headers.items()
               if k.lower() == 'set-cookie' and h.startswith('session=')]
    assert cookies, resp.headers
    cookie = cookies[0]

    # Flask hands Werkzeug an `expires` datetime (not `max_age`), so a
    # persistent cookie is identified by the Expires attribute alone.
    assert 'Expires=' in cookie, cookie

    expires = parsedate_to_datetime(cookie.split('Expires=')[1].split(';')[0])
    expected = datetime.now(timezone.utc) + app.permanent_session_lifetime
    assert abs((expires - expected).total_seconds()) < 60, cookie


def test_lifetime_is_bounded_and_configurable(app):
    """Not just 'has an expiry' — a *bounded* one, from SESSION_DAYS."""
    assert app.permanent_session_lifetime == timedelta(days=30)


def test_expired_cookie_is_rejected(app, client):
    """The signed timestamp is now enforced, so an old cookie stops working."""
    _create()
    _login(client)
    assert client.get('/admin/').status_code in (200, 403)  # session is live

    # Age the window past the cookie's signed timestamp.
    app.permanent_session_lifetime = timedelta(seconds=-1)
    resp = client.get('/admin/')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_logout_deletes_the_session_cookie(app, client):
    """Flask deletes the cookie itself when the emptied session is saved,
    with the configured name and flags; logout adds no cookie of its own."""
    _create()
    _login(client)
    resp = client.post('/logout')

    cookies = [h for k, h in resp.headers.items()
               if k.lower() == 'set-cookie' and h.startswith('session=')]
    assert len(cookies) == 1, cookies
    cookie = cookies[0]
    assert cookie.startswith('session=;') and 'Max-Age=0' in cookie, cookie
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie, cookie
    assert ('Secure' in cookie) == app.config['SESSION_COOKIE_SECURE'], cookie
    assert resp.headers['Location'].endswith('/login')
    assert client.get('/admin/').status_code == 302
