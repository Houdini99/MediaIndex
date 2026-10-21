"""All raw SQL for the WebDAV staging queue.

Staging rows mirror files sitting in staging/<user_id>/ (the per-user
WebDAV inbox). `fname` is the path relative to that user directory; the
absolute location comes from Config.STAGING_DIR. Rows live in the media
schema because they dedup against media.filehash and end their life as a
media insert.

Status lifecycle:
    pending     seen on disk, waiting to become size/mtime-stable
    ready       hashed + thumbnailed, awaiting user action in the queue
    duplicate   filehash already exists in media (reject-only in the UI)
    unsupported extension not in the allow-lists (reject-only in the UI)
    promoting   claimed by a promote request; reconciliation skips it
"""
import os
from datetime import datetime

from ..config import Config
from ..db_utils import db_conn, db_now, ensure_pg_schema


def _conn():
    return db_conn()


# ── Schema ─────────────────────────────────────────────────────────────────
def init_staging_db():
    ensure_pg_schema()


def staging_file_path(user_id, fname):
    """Absolute on-disk path for a staged file."""
    return os.path.join(Config.STAGING_DIR, str(user_id), fname)


def staging_thumb_path(thumb):
    """Absolute path in STAGING_THUMB_DIR for a stored thumb value."""
    return os.path.join(Config.STAGING_THUMB_DIR, os.path.basename(thumb))


# ── Scanner queries ────────────────────────────────────────────────────────
def insert_staging_row(user_id, fname, size, mtime):
    """Insert a new staging row; returns its id, or None when the
    (user_id, fname) row already exists (ON CONFLICT DO NOTHING never
    aborts the transaction)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO media.staging (user_id, fname, size, mtime, discovered_at) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id, fname) DO NOTHING RETURNING id",
            (user_id, fname, size, mtime, db_now()))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None


def update_staging_size(sid, size, mtime):
    """Record a new size/mtime observation (file still changing, or a
    processed file that was overwritten via DAV → back to pending).
    Never touches rows a promote request has claimed."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.staging SET size=%s, mtime=%s, status='pending', "
            "filehash=NULL, error=NULL WHERE id=%s AND status != 'promoting'",
            (size, mtime, sid))
        conn.commit()


def update_staging_processed(sid, filehash, thumb, ftype, status):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.staging SET filehash=%s, thumb=%s, ftype=%s, status=%s, "
            "error=NULL WHERE id=%s",
            (filehash, thumb, ftype, status, sid))
        conn.commit()


def set_staging_error(sid, message):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.staging SET status='error', error=%s WHERE id=%s",
            (message[:500] if message else 'processing failed', sid))
        conn.commit()


def all_rows_for_reconcile():
    """Every staging row as
    (id, user_id, fname, size, mtime, status, thumb, claimed_at)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, user_id, fname, size, mtime, status, thumb, claimed_at "
            "FROM media.staging")
        return cur.fetchall()


# ── Queue queries (ownership-scoped) ───────────────────────────────────────
_ROW_COLS = "id, fname, size, mtime, filehash, thumb, ftype, status, discovered_at"


def _row_dict(row):
    d = dict(zip(
        ('id', 'fname', 'size', 'mtime', 'filehash', 'thumb', 'ftype', 'status', 'discovered_at'),
        row,
    ))
    # timestamptz comes back as datetime; the queue JSON historically
    # carries an ISO string.
    if isinstance(d['discovered_at'], datetime):
        d['discovered_at'] = d['discovered_at'].isoformat()
    return d


def list_staging(user_id):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_ROW_COLS} FROM media.staging WHERE user_id=%s ORDER BY id DESC",
            (user_id,))
        return [_row_dict(r) for r in cur.fetchall()]


def get_staging_row(sid, user_id):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_ROW_COLS} FROM media.staging WHERE id=%s AND user_id=%s",
            (sid, user_id))
        row = cur.fetchone()
        return _row_dict(row) if row else None


def count_pending(user_id):
    """Items awaiting user action — everything not currently being promoted."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM media.staging WHERE user_id=%s AND status != 'promoting'",
            (user_id,))
        return cur.fetchone()[0]


# ── Promote lifecycle ──────────────────────────────────────────────────────
def claim_for_promote(sid, user_id):
    """Atomically claim a row for promotion. Only one request can win, and
    the scanner's reconciliation leaves claimed rows alone."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.staging SET status='promoting', claimed_at=%s "
            "WHERE id=%s AND user_id=%s AND status IN ('ready','duplicate')",
            (db_now(), sid, user_id))
        conn.commit()
        return cur.rowcount > 0


def release_claim(sid, status='ready'):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.staging SET status=%s, claimed_at=NULL "
            "WHERE id=%s AND status='promoting'",
            (status, sid))
        conn.commit()


def delete_staging_for_user(user_id):
    """Delete every staging row belonging to `user_id`; returns the thumb
    values of the deleted rows so the caller can remove the staged
    thumbnails from disk. Used by admin delete-user."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM media.staging WHERE user_id=%s RETURNING thumb",
            (user_id,))
        thumbs = [r[0] for r in cur.fetchall() if r[0]]
        conn.commit()
        return thumbs


def delete_staging_row(sid):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM media.staging WHERE id=%s", (sid,))
        conn.commit()
        return cur.rowcount > 0
