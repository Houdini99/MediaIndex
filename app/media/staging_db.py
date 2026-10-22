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

from psycopg.rows import dict_row

from ..config import Config
from ..db_utils import db_conn, db_now


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
    with db_conn() as conn:
        row = conn.execute(
            "INSERT INTO media.staging (user_id, fname, size, mtime, discovered_at) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id, fname) DO NOTHING RETURNING id",
            (user_id, fname, size, mtime, db_now())).fetchone()
        return row[0] if row else None


def update_staging_size(sid, size, mtime):
    """Record a new size/mtime observation (file still changing, or a
    processed file that was overwritten via DAV → back to pending).
    Never touches rows a promote request has claimed."""
    with db_conn() as conn:
        conn.execute(
            "UPDATE media.staging SET size=%s, mtime=%s, status='pending', "
            "filehash=NULL, error=NULL WHERE id=%s AND status != 'promoting'",
            (size, mtime, sid))


def update_staging_processed(sid, filehash, thumb, ftype, status):
    with db_conn() as conn:
        conn.execute(
            "UPDATE media.staging SET filehash=%s, thumb=%s, ftype=%s, status=%s, "
            "error=NULL WHERE id=%s",
            (filehash, thumb, ftype, status, sid))


def set_staging_error(sid, message):
    with db_conn() as conn:
        conn.execute(
            "UPDATE media.staging SET status='error', error=%s WHERE id=%s",
            (message[:500] if message else 'processing failed', sid))


def all_rows_for_reconcile():
    """Every staging row as
    (id, user_id, fname, size, mtime, status, thumb, claimed_at)."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, user_id, fname, size, mtime, status, thumb, claimed_at "
            "FROM media.staging").fetchall()


# ── Queue queries (ownership-scoped) ───────────────────────────────────────
_ROW_COLS = "id, fname, size, mtime, filehash, thumb, ftype, status, discovered_at"


def _iso_discovered(row):
    """timestamptz comes back as datetime; the queue JSON historically
    carries an ISO string."""
    if isinstance(row['discovered_at'], datetime):
        row['discovered_at'] = row['discovered_at'].isoformat()
    return row


def list_staging(user_id):
    with db_conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT {_ROW_COLS} FROM media.staging WHERE user_id=%s ORDER BY id DESC",
            (user_id,))
        return [_iso_discovered(r) for r in cur.fetchall()]


def get_staging_row(sid, user_id):
    with db_conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT {_ROW_COLS} FROM media.staging WHERE id=%s AND user_id=%s",
            (sid, user_id))
        row = cur.fetchone()
        return _iso_discovered(row) if row else None


def count_pending(user_id):
    """Items awaiting user action — everything not currently being promoted."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM media.staging WHERE user_id=%s AND status != 'promoting'",
            (user_id,)).fetchone()[0]


# ── Promote lifecycle ──────────────────────────────────────────────────────
def claim_for_promote(sid, user_id):
    """Atomically claim a row for promotion. Only one request can win, and
    the scanner's reconciliation leaves claimed rows alone."""
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE media.staging SET status='promoting', claimed_at=%s "
            "WHERE id=%s AND user_id=%s AND status IN ('ready','duplicate')",
            (db_now(), sid, user_id))
        return cur.rowcount > 0


def release_claim(sid, status='ready'):
    with db_conn() as conn:
        conn.execute(
            "UPDATE media.staging SET status=%s, claimed_at=NULL "
            "WHERE id=%s AND status='promoting'",
            (status, sid))


def delete_staging_for_user(user_id):
    """Delete every staging row belonging to `user_id`; returns the thumb
    values of the deleted rows so the caller can remove the staged
    thumbnails from disk. Used by admin delete-user."""
    with db_conn() as conn:
        rows = conn.execute(
            "DELETE FROM media.staging WHERE user_id=%s RETURNING thumb",
            (user_id,)).fetchall()
        return [r[0] for r in rows if r[0]]


def delete_staging_row(sid):
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM media.staging WHERE id=%s", (sid,))
        return cur.rowcount > 0
