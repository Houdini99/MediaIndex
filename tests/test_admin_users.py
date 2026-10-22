"""Admin user management: deletion, username rules, and session revocation.

Deleting a user was untested, and it was broken for anyone who had ever
used the Duplicates tab: dupe_job.started_by, dupe_dismissed.dismissed_by
and dupe_quarantine.marked_by referenced users.users with no ON DELETE, so
delete_user() raised a foreign-key violation — at the very end of
remove_user(), after that user's media and WebDAV inbox were already gone.
"""
import os
import tempfile

import pytest
from werkzeug.security import generate_password_hash

from app.auth.db import (
    fetch_user_by_id, fetch_user_for_login, insert_user, set_user_admin,
)
from app.config import Config
from app.db_utils import db_conn
from app.media import dupes_db
from app.media.db import get_media_by_hash
from app.media.ingest import ingest_file


def _login(client, username, password='password123'):
    resp = client.post('/login', data={'username': username, 'password': password})
    assert resp.status_code == 302
    return resp


def _create(username, admin=False, password='password123'):
    assert insert_user(username, generate_password_hash(password))
    uid = fetch_user_for_login(username)[0]
    set_user_admin(uid, admin)
    return uid


@pytest.fixture()
def admin(client):
    """Logged-in admin; returns its id."""
    uid = _create('root', admin=True)
    _login(client, 'root')
    return uid


def _png(color):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(buf, format='PNG')
    return buf.getvalue()


def _image(uid, color, private=False):
    fd, tmp = tempfile.mkstemp(dir=Config.UPLOAD_FOLDER, suffix='.png')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(_png(color))
    status, digest = ingest_file(tmp, 'png', 'x', uid, private)
    assert status == 'ok'
    return get_media_by_hash(digest)[0]


def _audit_columns():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT started_by FROM media.dupe_job")
        jobs = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT dismissed_by FROM media.dupe_dismissed")
        dismissed = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT marked_by FROM media.dupe_quarantine")
        marked = [r[0] for r in cur.fetchall()]
    return jobs, dismissed, marked


# ── Deletion ───────────────────────────────────────────────────────────────
def test_deleting_an_admin_with_duplicate_history(client, admin):
    """The actor columns outlive their actor as NULL; the delete completes."""
    bob = _create('bob', admin=True)
    a, b, c = (_image(admin, color) for color in ('red', 'green', 'blue'))
    job_id = dupes_db.claim_job('{}', 3, bob)
    dupes_db.finish_job(job_id, 'done')
    dupes_db.dismiss_pairs([(a, b)], bob)
    dupes_db.add_quarantine(c, a, 3, bob, 'x')

    resp = client.post(f'/admin/users/{bob}/delete',
                       data={'media_action': 'delete'})

    assert resp.status_code == 302
    assert fetch_user_by_id(bob) is None
    assert _audit_columns() == ([None], [None], [None])


def test_deleting_a_user_removes_their_media(client, admin):
    carol = _create('carol')
    mid = _image(carol, 'purple')
    resp = client.post(f'/admin/users/{carol}/delete',
                       data={'media_action': 'delete'})
    assert resp.status_code == 302
    assert fetch_user_by_id(carol) is None
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM media.media WHERE id=%s", (mid,))
        assert cur.fetchone() is None


def test_reassigned_private_media_opens_with_the_new_owners_key(client, admin):
    """The old owner's key is deleted with them, so their private file and
    thumbnail must be re-encrypted for the new owner, not merely handed
    over."""
    from app.auth.db import get_user_enc_key
    from app.media.crypto import iter_decrypted
    from app.media.db import get_media_by_id, media_path, thumb_path

    carol, dave = _create('carol'), _create('dave')
    mid = _image(carol, 'teal', private=True)

    resp = client.post(f'/admin/users/{carol}/delete',
                       data={'media_action': f'reassign:{dave}'})

    assert resp.status_code == 302
    row = get_media_by_id(mid)
    assert row['uploaded_by'] == dave and row['is_private']
    key = get_user_enc_key(dave)
    assert b''.join(iter_decrypted(media_path(row['filepath']), key)) == _png('teal')
    thumb = b''.join(iter_decrypted(thumb_path(row['thumb']), key))
    assert thumb.startswith(b'\x89PNG')


@pytest.mark.parametrize('target', ['self', 'missing', 'junk'])
def test_an_invalid_reassignment_target_deletes_nothing(client, admin, target):
    carol = _create('carol')
    mid = _image(carol, 'olive')
    action = {'self': f'reassign:{carol}', 'missing': 'reassign:99999',
              'junk': 'reassign:x'}[target]

    resp = client.post(f'/admin/users/{carol}/delete',
                       data={'media_action': action}, follow_redirects=True)

    assert b'Invalid reassignment target.' in resp.data
    assert fetch_user_by_id(carol) is not None
    from app.media.db import get_media_by_id
    assert get_media_by_id(mid) is not None


def test_user_routes_look_each_account_up_once(client, admin, monkeypatch):
    import app.admin.routes as admin_routes
    calls = []
    real = admin_routes.fetch_user_by_id
    monkeypatch.setattr(admin_routes, 'fetch_user_by_id',
                        lambda uid: calls.append(uid) or real(uid))
    carol, dave = _create('carol'), _create('dave')

    client.post(f'/admin/users/{carol}/delete',
                data={'media_action': f'reassign:{dave}'})
    assert calls == [carol, dave]

    calls.clear()
    client.post(f'/admin/users/{dave}/password', data={'password': 'long-enough'})
    assert calls == [dave]


def test_existing_databases_get_the_constraint_rewritten(app):
    """CREATE TABLE IF NOT EXISTS never revisits a table, so databases
    created before the fix carry the old NO ACTION constraints; boot must
    rewrite them."""
    import app.db_utils as db_utils
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE media.dupe_job DROP CONSTRAINT dupe_job_started_by_fkey")
        cur.execute("ALTER TABLE media.dupe_job ADD CONSTRAINT dupe_job_started_by_fkey "
                    "FOREIGN KEY (started_by) REFERENCES users.users(id)")
        conn.commit()
    db_utils._pg_schema_ready = False
    db_utils.ensure_pg_schema()

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid = 'users.users'::regclass "
            "AND conrelid::regclass::text LIKE 'media.dupe_%' ORDER BY conname")
        rows = cur.fetchall()
    assert rows == [('dupe_dismissed_dismissed_by_fkey', 'n'),
                    ('dupe_job_started_by_fkey', 'n'),
                    ('dupe_quarantine_marked_by_fkey', 'n')]


# ── Username rules ─────────────────────────────────────────────────────────
@pytest.mark.parametrize('name', ['a,b', 'a:b', 'has space', '', 'x' * 33, 'ümlaut'])
def test_admin_cannot_create_an_unusable_username(client, admin, name):
    resp = client.post('/admin/users/create',
                       data={'username': name, 'password': 'password123'},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert fetch_user_for_login(name) is None


@pytest.mark.parametrize('name', ['alice99', 'bob', 'Bot', 'first.last-2_x'])
def test_admin_can_create_ordinary_usernames(client, admin, name):
    client.post('/admin/users/create',
                data={'username': name, 'password': 'password123'})
    assert fetch_user_for_login(name) is not None


# ── Session revocation ─────────────────────────────────────────────────────
def test_password_reset_logs_out_existing_sessions(app, client, admin):
    alice = _create('alice')
    victim = app.test_client()          # e.g. a stolen cookie
    _login(victim, 'alice')
    assert victim.get('/').status_code == 200

    client.post(f'/admin/users/{alice}/password',
                data={'password': 'a-new-password'})

    resp = victim.get('/')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']
    # …and the new password works for a fresh login.
    _login(app.test_client(), 'alice', 'a-new-password')


def test_resetting_your_own_password_keeps_you_signed_in(app, client, admin):
    other_browser = app.test_client()
    _login(other_browser, 'root')

    client.post(f'/admin/users/{admin}/password',
                data={'password': 'a-new-password'})

    assert client.get('/').status_code == 200
    assert other_browser.get('/').status_code == 302


def test_sessions_from_before_generations_stay_valid(client, user):
    """Deploying the generation check must not log everyone out: a session
    issued before it carries no stamp and counts as generation 0."""
    uid, username, password = user
    _login(client, username, password)
    with client.session_transaction() as sess:
        sess.pop('gen', None)
    assert client.get('/').status_code == 200
