"""Discord account linking: link codes, and the bot's lookup.

A link is a round trip through Discord. /settings issues a one-time code to
the logged-in user (`POST /api/discord/link/code`); the user DMs it to the
bot; the bot redeems it with the sender's id as Discord reported it
(`POST /api/discord/verify`). So a link proves one person controls both
accounts — nobody can claim someone else's Discord id by typing it, which
the earlier "enter your id" form allowed.

What has to hold: codes work once, expire, are stored hashed, and are only
redeemable by the bot; and `GET /api/discord/user` answers only the bot.
"""
import base64
import re

from werkzeug.security import generate_password_hash

from app.auth.db import (
    claim_discord_link_code,
    fetch_user_for_login,
    get_discord_identity,
    hash_dav_password,
    insert_user,
    new_discord_link_code,
    resolve_discord_user,
    set_dav_password_hash,
    set_discord_identity,
)
from app.db_utils import db_conn

TOKEN = 'app-password-token-123'
SNOWFLAKE = '111111111111111111'


def _basic(username, password=TOKEN):
    raw = base64.b64encode(f'{username}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


def _account(username, with_token=True):
    insert_user(username, generate_password_hash('password123'))
    uid = fetch_user_for_login(username)[0]
    if with_token:
        set_dav_password_hash(uid, hash_dav_password(TOKEN))
    return uid


def _get_code(client):
    resp = client.post('/api/discord/link/code')
    assert resp.status_code == 200
    return resp.get_json()


def _verify(client, code, discord_id=SNOWFLAKE, as_user='Bot'):
    return client.post('/api/discord/verify',
                       data={'discord_id': discord_id, 'code': code},
                       headers=_basic(as_user))


def _lookup(client, discord_id, as_user='Bot'):
    return client.get('/api/discord/user',
                      query_string={'discord_id': discord_id},
                      headers=_basic(as_user))


# ── Issuing a code (/settings) ────────────────────────────────────────────
def test_a_code_is_issued_to_the_logged_in_user(client, logged_in, user):
    data = _get_code(client)
    assert re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', data['code'])
    assert data['expires_in'] == 600
    assert get_discord_identity(user[0]) is None     # nothing linked yet


def test_only_the_hash_of_a_code_is_stored(client, logged_in, user):
    code = _get_code(client)['code']
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT discord_link_code_hash FROM users.users "
                    "WHERE id=%s", (user[0],))
        stored = cur.fetchone()[0]
    assert stored and code.replace('-', '') not in stored


def test_a_new_code_replaces_the_old_one(client, logged_in, user):
    first = _get_code(client)['code']
    second = _get_code(client)['code']
    assert claim_discord_link_code(first, int(SNOWFLAKE)) is None
    assert claim_discord_link_code(second, int(SNOWFLAKE)) == 'alice'


def test_issuing_a_code_requires_a_login(client, app):
    assert client.post('/api/discord/link/code').status_code in (302, 401, 403)


def test_the_old_type_your_id_form_is_gone(client, logged_in, user):
    resp = client.post('/api/discord/link', data={'discord_id': SNOWFLAKE})
    assert resp.status_code in (404, 405)
    assert get_discord_identity(user[0]) is None


# ── Redeeming it (the bot) ────────────────────────────────────────────────
def test_the_bot_redeems_a_code_for_the_dm_sender(client, logged_in, user):
    _account('Bot')
    code = _get_code(client)['code']
    resp = _verify(client, code)
    assert resp.status_code == 200
    assert resp.get_json() == {'username': 'alice'}
    assert get_discord_identity(user[0]) == int(SNOWFLAKE)


def test_a_code_works_once(client, logged_in, user):
    _account('Bot')
    code = _get_code(client)['code']
    assert _verify(client, code).status_code == 200
    assert _verify(client, code, discord_id='222222222222222222') \
        .status_code == 404
    assert get_discord_identity(user[0]) == int(SNOWFLAKE)


def test_an_expired_code_is_refused(client, logged_in, user):
    _account('Bot')
    code = _get_code(client)['code']
    with db_conn() as conn:
        conn.cursor().execute(
            "UPDATE users.users SET discord_link_expires = now() - "
            "interval '1 second' WHERE id=%s", (user[0],))
        conn.commit()
    assert _verify(client, code).status_code == 404
    assert get_discord_identity(user[0]) is None


def test_a_wrong_code_is_refused(client, logged_in, user):
    _account('Bot')
    _get_code(client)
    assert _verify(client, 'AAAA-AAAA').status_code == 404
    assert _verify(client, '').status_code == 404
    assert get_discord_identity(user[0]) is None


def test_the_code_may_be_typed_loosely(client, logged_in, user):
    """Lower case, no dash, stray spaces — it came through a chat box."""
    _account('Bot')
    code = _get_code(client)['code']
    sloppy = ' ' + code.replace('-', '').lower() + ' '
    assert _verify(client, sloppy).status_code == 200


def test_redeeming_moves_a_link_from_another_account(client, logged_in, user):
    """Whoever redeems a code has proven control of both sides; an earlier
    link of the same Discord account does not outrank that."""
    _account('Bot')
    bob = _account('bob', with_token=False)
    set_discord_identity(bob, int(SNOWFLAKE))
    code = _get_code(client)['code']
    assert _verify(client, code).status_code == 200
    assert get_discord_identity(user[0]) == int(SNOWFLAKE)
    assert get_discord_identity(bob) is None


def test_only_a_delegated_account_may_redeem(client, logged_in, user):
    """Otherwise any user with a WebDAV app password could redeem a code
    with a Discord id of their choosing — the whole point is that the id
    comes from Discord, through the bot."""
    _account('carol')
    code = _get_code(client)['code']
    assert _verify(client, code, as_user='carol').status_code == 403
    client.post('/logout')
    resp = client.post('/api/discord/verify',
                       data={'discord_id': SNOWFLAKE, 'code': code})
    assert resp.status_code == 401
    assert get_discord_identity(user[0]) is None


def test_redeem_rejects_what_is_not_a_discord_id(client, logged_in, user):
    _account('Bot')
    code = _get_code(client)['code']
    for bad in ('', 'alice', '-5', '9' * 19):
        assert _verify(client, code, discord_id=bad).status_code == 400, bad
    # A refused request must not have spent the code.
    assert _verify(client, code).status_code == 200


# ── Status and unlink (/settings) ─────────────────────────────────────────
def test_status_reports_a_pending_code_then_the_link(client, logged_in, user):
    _account('Bot')
    assert client.get('/api/discord/status').get_json() == {
        'discord_id': None, 'pending': False}
    code = _get_code(client)['code']
    assert client.get('/api/discord/status').get_json()['pending'] is True
    _verify(client, code)
    # A string: 19 digits do not survive a JavaScript double.
    assert client.get('/api/discord/status').get_json() == {
        'discord_id': SNOWFLAKE, 'pending': False}


def test_unlink_clears_the_link_and_any_pending_code(client, logged_in, user):
    set_discord_identity(user[0], int(SNOWFLAKE))
    code = _get_code(client)['code']
    resp = client.post('/api/discord/unlink')
    assert resp.get_json() == {'status': 'unlinked'}
    assert get_discord_identity(user[0]) is None
    assert claim_discord_link_code(code, int(SNOWFLAKE)) is None


# ── The bot's lookup ──────────────────────────────────────────────────────
def test_lookup_returns_the_index_username(client, app, user):
    _account('Bot')
    set_discord_identity(user[0], int(SNOWFLAKE))
    resp = _lookup(client, SNOWFLAKE)
    assert resp.status_code == 200
    assert resp.get_json() == {'username': 'alice'}


def test_lookup_404s_for_an_unlinked_sender(client, app, user):
    _account('Bot')
    assert _lookup(client, SNOWFLAKE).status_code == 404


def test_lookup_requires_authentication(client, app):
    resp = client.get('/api/discord/user',
                      query_string={'discord_id': SNOWFLAKE})
    assert resp.status_code == 401
    assert 'WWW-Authenticate' in resp.headers


def test_lookup_refuses_a_non_delegated_account(client, app, user):
    _account('carol')
    assert _lookup(client, SNOWFLAKE, as_user='carol').status_code == 403


def test_lookup_rejects_what_is_not_an_id(client, app, user):
    _account('Bot')
    for bad in ('', 'alice', '-5', '9' * 19):
        assert _lookup(client, bad).status_code == 400, bad


def test_resolve_matches_on_the_id(app, user):
    set_discord_identity(user[0], int(SNOWFLAKE))
    assert resolve_discord_user(int(SNOWFLAKE)) == 'alice'
    assert resolve_discord_user(1) is None


def test_new_codes_are_not_predictable(app, user):
    codes = {new_discord_link_code(user[0]) for _ in range(50)}
    assert len(codes) == 50


# ── The settings card ─────────────────────────────────────────────────────
def test_settings_shows_the_discord_card_unlinked(client, logged_in, user):
    html = client.get('/settings').get_data(as_text=True)
    assert 'Connect Discord' in html
    assert 'Get link code' in html
    assert 'Connect Telegram' in html                # still beside it


def test_settings_shows_a_linked_id(client, logged_in, user):
    set_discord_identity(user[0], int(SNOWFLAKE))
    html = client.get('/settings').get_data(as_text=True)
    assert f'Linked to ID {SNOWFLAKE}' in html
    assert 'Link a different account' in html
