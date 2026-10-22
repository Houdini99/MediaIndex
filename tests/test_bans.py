"""Failed-login tracking: threshold ban, expiry, count decay, manual bans."""
from datetime import datetime, timedelta

from app.config import Config
from app.auth.db import (
    record_failed_login, is_ip_banned, reset_failed_logins,
    ban_ip, unban_ip, fetch_banned_ips,
)
from app.db_utils import db_conn
IP = '203.0.113.7'


def _set_last_attempt(ip, dt):
    # Naive local datetime from the tests; timestamptz wants it aware.
    with db_conn() as conn:
        conn.execute("UPDATE users.failed_logins SET last_attempt=%s WHERE ip=%s",
                     (dt.astimezone(), ip))
        conn.commit()


def _row_count(ip):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users.failed_logins WHERE ip=%s", (ip,))
        return cur.fetchone()[0]


def _fail_count(ip):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count FROM users.failed_logins WHERE ip=%s", (ip,))
        return cur.fetchone()[0]


def test_ban_after_threshold(app):
    for _ in range(Config.BAN_THRESHOLD - 1):
        record_failed_login(IP)
    assert not is_ip_banned(IP)
    record_failed_login(IP)
    assert is_ip_banned(IP)


def test_auto_ban_expires(app):
    for _ in range(Config.BAN_THRESHOLD):
        record_failed_login(IP)
    assert is_ip_banned(IP)
    _set_last_attempt(IP, datetime.now() - Config.BAN_DURATION - timedelta(minutes=1))
    assert not is_ip_banned(IP)
    # expiry also reset the failure count (row deleted)
    assert _row_count(IP) == 0


def test_failure_count_decays(app):
    for _ in range(Config.BAN_THRESHOLD - 1):
        record_failed_login(IP)
    _set_last_attempt(IP, datetime.now() - Config.FAIL_WINDOW - timedelta(minutes=1))
    record_failed_login(IP)  # stale streak → counter restarts at 1
    assert not is_ip_banned(IP)
    assert _fail_count(IP) == 1


def test_manual_ban_never_expires(app):
    ban_ip(IP)
    assert is_ip_banned(IP)
    _set_last_attempt(IP, datetime.now() - Config.BAN_DURATION * 10)
    assert is_ip_banned(IP)
    assert unban_ip(IP)
    assert not is_ip_banned(IP)


def test_successful_login_resets(app):
    for _ in range(3):
        record_failed_login(IP)
    reset_failed_logins(IP)
    assert _row_count(IP) == 0


def test_banned_ip_blocked_at_http_layer(client, app):
    ban_ip('127.0.0.1')
    assert client.get('/login').status_code == 403


def test_fetch_banned_ips_marks_manual(app):
    ban_ip(IP)
    rows = fetch_banned_ips()
    assert len(rows) == 1
    ip, count, last_attempt, manual = rows[0]
    assert ip == IP and manual == 1
