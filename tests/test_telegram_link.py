"""Telegram account linking: the website form and the bot's ingest gate.

Two endpoints, one identity model:

* `POST /api/telegram/link` is the user-facing form on /settings.
* `GET /api/telegram/user` is what the bot calls before it downloads
  anything, and is restricted to DELEGATED_UPLOADERS.

The interesting behaviour is in between them. A numeric Telegram id is
immutable and is the real identity; an @username can be released by its
owner and re-registered by a stranger, who would then inherit that user's
ingest path. So a username match is allowed exactly once, and pins the
sender's numeric id — every later message matches on the number instead.
These tests pin that, plus the uniqueness that stops two index accounts
claiming the same Telegram user.
"""
import base64

from werkzeug.security import generate_password_hash

from app.auth.db import (
    fetch_user_for_login,
    get_telegram_identity,
    hash_dav_password,
    insert_user,
    resolve_telegram_user,
    set_dav_password_hash,
    set_telegram_identity,
)

TOKEN = 'app-password-token-123'


def _basic(username, password=TOKEN):
    raw = base64.b64encode(f'{username}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


def _account(username, with_token=True):
    insert_user(username, generate_password_hash('password123'))
    uid = fetch_user_for_login(username)[0]
    if with_token:
        set_dav_password_hash(uid, hash_dav_password(TOKEN))
    return uid


def _lookup(client, telegram_id, username=None, as_user='Bot'):
    params = {'telegram_id': telegram_id}
    if username:
        params['username'] = username
    return client.get('/api/telegram/user', query_string=params,
                      headers=_basic(as_user))


# ── The form ──────────────────────────────────────────────────────────────
def test_link_by_numeric_id(client, logged_in, user):
    resp = client.post('/api/telegram/link', data={'identity': '123456789'})
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'linked', 'telegram_id': 123456789,
                               'telegram_username': None, 'pinned': True}
    assert get_telegram_identity(user[0]) == (123456789, None)


def test_link_by_username_strips_the_at_sign(client, logged_in, user):
    resp = client.post('/api/telegram/link', data={'identity': '@alice_tg'})
    assert resp.status_code == 200
    assert resp.get_json()['telegram_username'] == 'alice_tg'
    assert resp.get_json()['pinned'] is False
    assert get_telegram_identity(user[0]) == (None, 'alice_tg')


def test_link_rejects_an_unusable_username(client, logged_in, user):
    """A value that can never match a real sender must not be stored: on
    the settings page it would look exactly like a working link and simply
    never ingest."""
    resp = client.post('/api/telegram/link', data={'identity': 'no!'})
    assert resp.status_code == 400
    assert get_telegram_identity(user[0]) == (None, None)


def test_unlink_clears_both_columns(client, logged_in, user):
    client.post('/api/telegram/link', data={'identity': '123456789'})
    resp = client.post('/api/telegram/link', data={'identity': ''})
    assert resp.get_json()['status'] == 'unlinked'
    assert get_telegram_identity(user[0]) == (None, None)


def test_relinking_to_a_username_clears_the_pinned_id(client, logged_in, user):
    """Otherwise the old pin keeps matching the old Telegram account no
    matter what the form says."""
    client.post('/api/telegram/link', data={'identity': '123456789'})
    client.post('/api/telegram/link', data={'identity': 'someone_else'})
    assert get_telegram_identity(user[0]) == (None, 'someone_else')


def test_two_accounts_cannot_claim_the_same_telegram_id(client, logged_in,
                                                        user, app):
    other = _account('bob', with_token=False)
    set_telegram_identity(other, 123456789, None)
    resp = client.post('/api/telegram/link', data={'identity': '123456789'})
    assert resp.status_code == 409
    assert get_telegram_identity(user[0]) == (None, None)


def test_linking_requires_a_login(client, app):
    resp = client.post('/api/telegram/link', data={'identity': '1'})
    assert resp.status_code in (302, 401, 403)


# ── resolve_telegram_user: the id/username precedence ─────────────────────
def test_id_match_wins_and_reports_pinned(app, user):
    set_telegram_identity(user[0], 555, None)
    assert resolve_telegram_user(555, 'anything') == ('alice', True)


def test_username_match_pins_the_numeric_id(app, user):
    """The mitigation for username takeover: after the first match the row
    identifies its owner by a number a stranger cannot obtain."""
    set_telegram_identity(user[0], None, 'alice_tg')
    assert resolve_telegram_user(777, 'alice_tg') == ('alice', True)
    assert get_telegram_identity(user[0]) == (777, 'alice_tg')


def test_a_released_username_stops_matching_once_pinned(app, user):
    """The takeover attempt: someone else registers @alice_tg and messages
    the bot. The pin means their id does not match, and the username no
    longer matches either."""
    set_telegram_identity(user[0], None, 'alice_tg')
    assert resolve_telegram_user(777, 'alice_tg') == ('alice', True)
    assert resolve_telegram_user(999, 'alice_tg') is None


def test_unknown_sender_resolves_to_nothing(app, user):
    set_telegram_identity(user[0], 555, None)
    assert resolve_telegram_user(556) is None
    assert resolve_telegram_user(556, 'nobody') is None


def test_username_match_is_case_insensitive(app, user):
    set_telegram_identity(user[0], None, 'Alice_TG')
    assert resolve_telegram_user(777, 'alice_tg') == ('alice', True)


# ── The bot's lookup endpoint ─────────────────────────────────────────────
def test_lookup_returns_the_index_username(client, app, user):
    _account('Bot')
    set_telegram_identity(user[0], 555, None)
    resp = _lookup(client, 555)
    assert resp.status_code == 200
    assert resp.get_json() == {'username': 'alice', 'pinned': True}


def test_lookup_404s_for_an_unlinked_sender(client, app, user):
    _account('Bot')
    resp = _lookup(client, 4242)
    assert resp.status_code == 404


def test_lookup_requires_authentication(client, app):
    resp = client.get('/api/telegram/user', query_string={'telegram_id': 1})
    assert resp.status_code == 401
    assert 'WWW-Authenticate' in resp.headers


def test_lookup_refuses_a_non_delegated_account(client, app, user):
    """An ordinary user's WebDAV app password must not map Telegram
    identities onto index usernames — that is the bot's business only."""
    _account('carol')
    resp = _lookup(client, 555, as_user='carol')
    assert resp.status_code == 403


def test_lookup_rejects_a_non_numeric_id(client, app, user):
    _account('Bot')
    resp = _lookup(client, 'not-a-number')
    assert resp.status_code == 400


def test_lookup_pins_through_the_endpoint(client, app, user):
    """The pin must happen server-side — the bot has no database."""
    _account('Bot')
    set_telegram_identity(user[0], None, 'alice_tg')
    assert _lookup(client, 777, 'alice_tg').status_code == 200
    assert get_telegram_identity(user[0]) == (777, 'alice_tg')
    assert _lookup(client, 999, 'alice_tg').status_code == 404


# ── The settings card on /settings ────────────────────────────────────────
# Cheap, but it is the only thing that would catch the template and the
# route disagreeing about a variable name — which fails silently, as an
# empty box that never links anything.
def test_settings_shows_the_telegram_card_unlinked(client, logged_in, user):
    html = client.get('/settings').get_data(as_text=True)
    assert 'Connect Telegram' in html
    assert 'Not linked' in html
    assert 'tgIdentity' in html


def test_settings_shows_a_pinned_id(client, logged_in, user):
    set_telegram_identity(user[0], 123456789, None)
    html = client.get('/settings').get_data(as_text=True)
    assert 'Linked to ID 123456789' in html
    assert 'value="123456789"' in html


def test_settings_marks_a_username_link_unverified(client, logged_in, user):
    """The word matters — it is the user-visible half of the reason the
    numeric ID is recommended."""
    set_telegram_identity(user[0], None, 'alice_tg')
    html = client.get('/settings').get_data(as_text=True)
    assert 'Linked to @alice_tg (unverified)' in html


def test_settings_requires_a_login(client, app):
    resp = client.get('/settings')
    assert resp.status_code in (302, 401, 403)


def test_queue_no_longer_carries_the_telegram_card(client, logged_in, user):
    """The link form lives on /settings now; leaving a second copy behind
    would mean two elements with the same ids and one dead form."""
    html = client.get('/queue').get_data(as_text=True)
    assert 'tgIdentity' not in html
