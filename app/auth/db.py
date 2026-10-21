"""All raw SQL for the auth domain.

Grouped here so that route handlers in routes.py never embed SQL.
PostgreSQL-only: schema-qualified names (users.users, users.failed_logins),
real booleans, timestamptz values.
"""
import hashlib
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from psycopg.errors import UniqueViolation

from ..config import Config
from ..db_utils import db_conn, db_now, ensure_pg_schema


def _conn():
    return db_conn()


#: Advisory-lock key serializing user creation, so the "first account is an
#: admin" decision cannot be made twice. Distinct from the schema-init key
#: in app.db_utils.
_BOOTSTRAP_LOCK_KEY = 0x6D656469_62


def init_auth_db():
    ensure_pg_schema()


# ── User lookup ────────────────────────────────────────────────────────────
def fetch_user_by_id(user_id):
    """(id, username, is_admin, session_gen, must_change_password), or None."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, is_admin, session_gen, "
                    "must_change_password FROM users.users WHERE id=%s",
                    (user_id,))
        return cur.fetchone()


def fetch_user_for_login(username):
    """(id, password_hash, session_gen), or None."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, password_hash, session_gen FROM users.users "
                    "WHERE username = %s", (username,))
        return cur.fetchone()


def insert_user(username, password_hash, enc_key=None, must_change_password=False):
    """Create a user. The very first account ever created becomes an admin
    automatically (bootstrap); all later grants happen in the admin panel.
    `must_change_password` makes the user pick their own password at their
    first login (accounts an admin creates).

    Returns False when the username is already taken. ON CONFLICT DO
    NOTHING (rather than catching the unique violation) so the connection
    is never left in an aborted transaction.
    """
    with _conn() as conn:
        cur = conn.cursor()
        # Serialize user creation. Reading COUNT(*) and passing the result
        # back into the INSERT was a read-then-write race — two
        # registrations against an empty table both saw zero and both became
        # admin.
        #
        # Folding the test into the INSERT as `NOT EXISTS (…)` is NOT enough
        # on its own, which is worth stating because it looks sufficient:
        # under READ COMMITTED each transaction evaluates the subquery
        # against a snapshot that excludes the other's uncommitted row, so
        # both still see an empty table. Measured — two concurrent inserts
        # granted admin 2 times out of 2.
        #
        # The advisory lock is what actually closes it: it is held to the end
        # of the transaction, so the second caller blocks until the first has
        # committed and then sees the row. User creation is rare, so
        # serializing it costs nothing.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_BOOTSTRAP_LOCK_KEY,))
        cur.execute(
            "INSERT INTO users.users (username, password_hash, enc_key, "
            "must_change_password, is_admin) "
            "VALUES (%s, %s, %s, %s, NOT EXISTS (SELECT 1 FROM users.users)) "
            "ON CONFLICT (username) DO NOTHING RETURNING id",
            (username, password_hash, enc_key, bool(must_change_password)))
        created = cur.fetchone() is not None
        conn.commit()
        return created


# ── Per-user media encryption key ─────────────────────────────────────────
def get_user_enc_key(user_id):
    """Return the user's raw encryption key bytes, or None if unset."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT enc_key FROM users.users WHERE id=%s", (user_id,))
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    return bytes(row[0])


def get_or_create_user_enc_key(user_id):
    """Return the user's encryption key, lazily generating one on first use.

    Existing users created before per-user encryption was added get a key
    minted the first time they upload a private file.
    """
    existing = get_user_enc_key(user_id)
    if existing:
        return existing
    # Local import to avoid pulling cryptography into the auth module
    # unless we actually need it.
    from ..media.crypto import new_key
    key = new_key()
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users.users SET enc_key=%s WHERE id=%s",
                    (key, user_id))
        conn.commit()
    return key


# ── WebDAV app password ────────────────────────────────────────────────────
# The app password is a generated high-entropy token (secrets.token_urlsafe),
# so it is stored as a plain SHA-256 digest: a random 192-bit token cannot be
# dictionary-attacked, and DAV clients authenticate on every request, so
# verification must be cheap (a slow KDF here would throttle all DAV I/O).
def hash_dav_password(password):
    import hashlib
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def set_dav_password_hash(user_id, password_hash):
    """Set (or revoke, with None) the user's WebDAV app-password digest."""
    with _conn() as conn:
        cur = conn.cursor()
        created = db_now() if password_hash else None
        cur.execute(
            "UPDATE users.users SET dav_password_hash=%s, dav_password_created=%s "
            "WHERE id=%s",
            (password_hash, created, user_id))
        conn.commit()
        return cur.rowcount > 0


def fetch_user_for_dav(username):
    """Return (id, dav_password_hash) for a username, or None."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, dav_password_hash FROM users.users WHERE username=%s",
                    (username,))
        return cur.fetchone()


def get_dav_password_created(user_id):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT dav_password_created FROM users.users WHERE id=%s",
                    (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


# ── Telegram ingest identity ───────────────────────────────────────────────
# The bot never touches this database — it asks the web app over HTTP (see
# GET /api/telegram/user), which is why the resolve-and-pin logic lives
# here rather than in the bot.
def resolve_telegram_user(telegram_id, telegram_username=None):
    """Resolve a Telegram sender to the index account that claimed it.

    Returns (username, pinned) or None when nobody has linked this
    account. `pinned` reports whether the match was made on the immutable
    numeric id rather than on the mutable @username.

    The numeric id wins whenever it is stored. Only if no row claims the
    id do we fall back to the username — and then the sender's id is
    written into that row, so every later message from the same account
    matches on the id instead. That trust-on-first-use pin is the whole
    mitigation for username takeover: a @handle released after the pin
    lands is worthless to whoever picks it up, because the row it used to
    match now identifies its owner by a number they cannot obtain.
    """
    if telegram_id is None:
        return None
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT username FROM users.users WHERE telegram_user_id = %s",
            (int(telegram_id),))
        row = cur.fetchone()
        if row:
            return row[0], True
        name = (telegram_username or '').strip().lstrip('@')
        if not name:
            return None
        # Pin in the same statement that matches, so two messages arriving
        # together cannot both read "unpinned" and race on the UPDATE. The
        # id is only claimed while the row still has none — a row that was
        # pinned in between keeps its existing id and simply does not
        # match here, which is the safe direction to fail.
        cur.execute(
            """
            UPDATE users.users SET telegram_user_id = %s
            WHERE lower(telegram_username) = lower(%s)
              AND telegram_user_id IS NULL
            RETURNING username
            """,
            (int(telegram_id), name))
        row = cur.fetchone()
        conn.commit()
        return (row[0], True) if row else None


def get_telegram_identity(user_id):
    """(telegram_user_id, telegram_username) for a user, or (None, None)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT telegram_user_id, telegram_username FROM users.users "
            "WHERE id=%s", (user_id,))
        row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)


def set_telegram_identity(user_id, telegram_id, telegram_username):
    """Link (or, with both values None, unlink) a Telegram account.

    Returns 'ok', or 'taken' when another index user already claims that
    id or username — the partial unique indexes make that a constraint
    violation rather than a silent second claim, and the caller turns it
    into a message instead of a 500.

    Writing a username always clears any pinned id, so a deliberate
    re-link to a different Telegram account works; without that, the old
    pin would keep matching the old account forever.
    """
    name = (telegram_username or '').strip().lstrip('@') or None
    tg_id = int(telegram_id) if telegram_id else None
    with _conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE users.users SET telegram_user_id=%s, telegram_username=%s "
                "WHERE id=%s", (tg_id, name, user_id))
        except UniqueViolation:
            # Rolled back explicitly: psycopg leaves the transaction
            # aborted after the error, and db_conn() commits on a clean
            # exit — returning without this would try to commit a dead
            # transaction from inside the pool.
            conn.rollback()
            return 'taken'
        conn.commit()
        return 'ok'


# ── Discord ingest identity ────────────────────────────────────────────────
# Same split as Telegram: the bot asks over HTTP (GET /api/discord/user,
# POST /api/discord/verify). The id is only ever set from a link code the
# bot hands back, never typed in, so there is nothing to pin.

#: No look-alikes (0/O, 1/I/L). 8 characters is ~40 bits — against codes
#: that live 10 minutes and can only be tried through Discord DMs.
_LINK_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
DISCORD_LINK_TTL = 600


def _link_code_hash(code):
    """sha256 of a code as the user may type it back: any case, with or
    without the dash or stray spaces. None for an empty code."""
    norm = re.sub(r'[\s-]', '', code or '').upper()
    return hashlib.sha256(norm.encode()).hexdigest() if norm else None


def resolve_discord_user(discord_id):
    """Index username that linked this Discord account, or None."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT username FROM users.users WHERE discord_user_id = %s",
            (int(discord_id),))
        row = cur.fetchone()
        return row[0] if row else None


def get_discord_identity(user_id):
    """The Discord user id a user linked, or None."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT discord_user_id FROM users.users WHERE id=%s",
                    (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def discord_link_pending(user_id):
    """True while a link code this user was issued is unredeemed and live."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT coalesce(discord_link_expires > now(), false) "
            "FROM users.users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        return bool(row and row[0])


def set_discord_identity(user_id, discord_id):
    """Set (or, with None, remove) a Discord link directly, without a code,
    and drop any pending code. Unlinking and tests only — a link the user
    asks for goes through new_discord_link_code / claim_discord_link_code.
    Returns 'ok', or 'taken' when another index user holds that id."""
    with _conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE users.users SET discord_user_id=%s, "
                "discord_link_code_hash=NULL, discord_link_expires=NULL "
                "WHERE id=%s",
                (int(discord_id) if discord_id else None, user_id))
        except UniqueViolation:
            # Same reason as set_telegram_identity: the transaction is
            # aborted and must not be committed on the way out.
            conn.rollback()
            return 'taken'
        conn.commit()
        return 'ok'


def new_discord_link_code(user_id):
    """Issue a one-time code proving this index account, replacing any
    earlier one; returns it formatted for display (`K7QX-M2PA`). Only the
    hash is stored."""
    code = ''.join(secrets.choice(_LINK_ALPHABET) for _ in range(8))
    with _conn() as conn:
        conn.cursor().execute(
            "UPDATE users.users SET discord_link_code_hash=%s, "
            "discord_link_expires=now() + make_interval(secs => %s) "
            "WHERE id=%s",
            (_link_code_hash(code), DISCORD_LINK_TTL, user_id))
        conn.commit()
    return f'{code[:4]}-{code[4:]}'


def claim_discord_link_code(code, discord_id):
    """Redeem a link code on behalf of the Discord account that sent it:
    link `discord_id` to the index account that issued the code and return
    that account's username. None when the code is wrong, used or expired.

    Consumed in the same statement that matches it, so a code works once
    even when two DMs carrying it race. If this Discord account was linked
    to a different index account, the link moves: whoever redeems a code
    has proven control of both sides, which no earlier link outranks.
    """
    code_hash = _link_code_hash(code)
    if code_hash is None:
        return None
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users.users
               SET discord_link_code_hash = NULL, discord_link_expires = NULL
             WHERE discord_link_code_hash = %s AND discord_link_expires > now()
            RETURNING id, username
            """, (code_hash,))
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        uid, username = row
        try:
            cur.execute(
                "UPDATE users.users SET discord_user_id = NULL "
                "WHERE discord_user_id = %s AND id <> %s",
                (int(discord_id), uid))
            cur.execute(
                "UPDATE users.users SET discord_user_id = %s WHERE id = %s",
                (int(discord_id), uid))
        except UniqueViolation:
            # Only reachable by two codes for the same Discord account
            # landing at once; the loser's code survives to be resent.
            conn.rollback()
            return None
        conn.commit()
        return username


# ── Failed-login / IP-ban tracking ─────────────────────────────────────────
_ban_check_err_last = 0.0


def is_ip_banned(ip):
    """True if `ip` is currently banned.

    Manual admin bans are permanent. Automatic threshold bans expire after
    Config.BAN_DURATION; expired ones are deleted here so the failure count
    also resets.

    Fails open by design (availability over enforcement): any DB error
    means "not banned" — but is logged (rate-limited) so a dead database
    can't silently disable bans without a trace.
    """
    global _ban_check_err_last
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT banned, manual, last_attempt FROM users.failed_logins "
                "WHERE ip=%s", (ip,))
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            banned, manual, last_attempt = row
            if manual:
                return True
            if (last_attempt is not None
                    and datetime.now(timezone.utc) - last_attempt < Config.BAN_DURATION):
                return True
            # Auto-ban expired (or timestamp missing) — lift it.
            cur.execute("DELETE FROM users.failed_logins WHERE ip=%s AND NOT manual",
                        (ip,))
            conn.commit()
            return False
    except Exception:
        now = time.monotonic()
        if now - _ban_check_err_last > 60:
            _ban_check_err_last = now
            logging.exception("is_ip_banned failed — bans are NOT being enforced")
        return False


def record_failed_login(ip):
    """Count a failed login for `ip`; auto-ban at Config.BAN_THRESHOLD.

    The failure count decays: if the previous attempt is older than
    Config.FAIL_WINDOW the counter restarts at 1 instead of accumulating
    forever (so a household behind NAT can't be banned by stale history).

    One atomic upsert: two workers racing on the same IP can never collide
    into a unique violation the way read-then-write could.

    Returns True when this failure is the one that banned the address
    (for the security log). A banned address is refused before it can fail
    again, so the count reaches the threshold exactly once per ban.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users.failed_logins (ip, count, banned, manual, last_attempt)
            VALUES (%(ip)s, 1, 1 >= %(thresh)s, false, %(now)s)
            ON CONFLICT (ip) DO UPDATE SET
                count = CASE
                    WHEN failed_logins.last_attempt IS NULL
                         OR excluded.last_attempt - failed_logins.last_attempt > %(window)s
                    THEN 1 ELSE failed_logins.count + 1 END,
                banned = CASE
                    WHEN failed_logins.manual THEN true
                    WHEN failed_logins.last_attempt IS NULL
                         OR excluded.last_attempt - failed_logins.last_attempt > %(window)s
                    THEN 1 >= %(thresh)s
                    ELSE failed_logins.banned
                         OR failed_logins.count + 1 >= %(thresh)s END,
                last_attempt = excluded.last_attempt
            RETURNING banned AND NOT manual AND count = %(thresh)s
            """,
            {'ip': ip, 'thresh': Config.BAN_THRESHOLD,
             'window': Config.FAIL_WINDOW, 'now': db_now()},
        )
        started_ban = bool(cur.fetchone()[0])
        conn.commit()
        return started_ban


def reset_failed_logins(ip):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users.failed_logins WHERE ip=%s", (ip,))
        conn.commit()


# ── Admin: user management ─────────────────────────────────────────────────
def fetch_all_users():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, is_admin FROM users.users ORDER BY id")
        return cur.fetchall()


def delete_user(user_id):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users.users WHERE id=%s", (user_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def set_user_admin(user_id, is_admin):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users.users SET is_admin=%s WHERE id=%s",
                    (bool(is_admin), user_id))
        updated = cur.rowcount > 0
        conn.commit()
        return updated


def count_users():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users.users")
        return cur.fetchone()[0]


def update_user_password(user_id, password_hash, must_change_password=False):
    """Replace the password and end every existing login of this user.

    A reset is exactly what someone does after a password or a device
    leaked, so it bumps session_gen: each browser still holding an old
    session cookie is logged out on its next request (see
    load_logged_in_user). Covers the admin panel and both CLI scripts.

    `must_change_password=True` makes the user choose their own password
    at their next login (an admin resetting someone else's). False leaves
    the flag as it was, so a reset never lets a user skip that step.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users.users SET password_hash=%s, "
                    "must_change_password = must_change_password OR %s, "
                    "session_gen = session_gen + 1 WHERE id=%s",
                    (password_hash, bool(must_change_password), user_id))
        updated = cur.rowcount > 0
        conn.commit()
        return updated


def change_own_password(user_id, password_hash):
    """A user replacing their own password: like update_user_password it
    ends every other login, and it also clears must_change_password.

    Returns the new session_gen, for the caller to stamp into the session
    that made the change, or None if the user no longer exists.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users.users SET password_hash=%s, "
                    "must_change_password=false, "
                    "session_gen = session_gen + 1 WHERE id=%s "
                    "RETURNING session_gen",
                    (password_hash, user_id))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None


# ── Admin: IP-ban management ───────────────────────────────────────────────
def fetch_banned_ips():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ip, count, last_attempt, manual FROM users.failed_logins "
            "WHERE banned ORDER BY last_attempt DESC")
        return cur.fetchall()


def unban_ip(ip):
    """Lift a ban by deleting the row, which also resets the failure count."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users.failed_logins WHERE ip=%s AND banned",
                    (ip,))
        removed = cur.rowcount > 0
        conn.commit()
        return removed


def fetch_watchlist_ips():
    """IPs with failed attempts that are not (yet) banned, worst first."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ip, count, last_attempt FROM users.failed_logins "
            "WHERE NOT banned AND count > 0 ORDER BY count DESC, last_attempt DESC")
        return cur.fetchall()


def ban_ip(ip):
    """Manually ban an IP (permanent until lifted), keeping any existing
    failure count."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users.failed_logins (ip, count, banned, manual, last_attempt)
            VALUES (%s, 0, true, true, %s)
            ON CONFLICT (ip) DO UPDATE SET banned = true, manual = true
            """,
            (ip, db_now()))
        conn.commit()
