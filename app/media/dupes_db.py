"""All raw SQL for the near-duplicate finder.

Five tables, all in the media schema (see the DDL in app/db_utils.py):

    media_hash       one row per sampled frame — the fingerprint index
    media_hash_meta  per-media bookkeeping: source, algo version, size
    dupe_dismissed   pairs an admin marked "not duplicates"
    dupe_quarantine  step one of the two-step resolve
    dupe_job         scan progress, shared across gunicorn workers

Private media is never hashed and never returned by anything here: every
read joins media.media and filters `NOT is_private`.

Perceptual hashes are 64-bit unsigned; Postgres bigint is signed, so they
round-trip through to_signed/to_unsigned at the boundary.
"""
import logging

from psycopg.rows import namedtuple_row

from ..db_utils import db_conn, db_now, ensure_pg_schema

_log = logging.getLogger(__name__)

_OFFSET = 1 << 63
_MASK64 = (1 << 64) - 1


def _conn():
    return db_conn()


def init_dupes_db():
    ensure_pg_schema()


# ── 64-bit unsigned ↔ signed bigint ────────────────────────────────────────
def to_signed(value):
    return (value & _MASK64) - _OFFSET


def to_unsigned(value):
    return (value + _OFFSET) & _MASK64


# ── Hash index: writes ─────────────────────────────────────────────────────
def store_hashes(media_id, frames, source, algo_ver, width, height, bytes_,
                 ftype):
    """Replace every stored frame hash for one media row.

    `frames` is a list of (phash, dhash, ahash) unsigned 64-bit tuples in
    sample order.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM media.media_hash WHERE media_id=%s", (media_id,))
        cur.executemany(
            "INSERT INTO media.media_hash (media_id, frame_idx, phash, dhash, ahash) "
            "VALUES (%s,%s,%s,%s,%s)",
            [(media_id, idx, to_signed(p), to_signed(d), to_signed(a))
             for idx, (p, d, a) in enumerate(frames)])
        cur.execute(
            "INSERT INTO media.media_hash_meta "
            "(media_id, source, algo_ver, frames, width, height, bytes, ftype, hashed_at, error) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL) "
            "ON CONFLICT (media_id) DO UPDATE SET "
            "source=EXCLUDED.source, algo_ver=EXCLUDED.algo_ver, "
            "frames=EXCLUDED.frames, width=EXCLUDED.width, height=EXCLUDED.height, "
            "bytes=EXCLUDED.bytes, ftype=EXCLUDED.ftype, "
            "hashed_at=EXCLUDED.hashed_at, error=NULL",
            (media_id, source, algo_ver, len(frames), width, height, bytes_,
             ftype, db_now()))
        conn.commit()


def store_hash_error(media_id, source, algo_ver, ftype, error):
    """Record a media row that could not be fingerprinted.

    The meta row is kept (with frames=0) so incremental scans do not retry
    a permanently broken file on every run; its stale frame hashes, if any,
    are dropped so it cannot keep matching under an old fingerprint.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM media.media_hash WHERE media_id=%s", (media_id,))
        cur.execute(
            "INSERT INTO media.media_hash_meta "
            "(media_id, source, algo_ver, frames, ftype, hashed_at, error) "
            "VALUES (%s,%s,%s,0,%s,%s,%s) "
            "ON CONFLICT (media_id) DO UPDATE SET "
            "source=EXCLUDED.source, algo_ver=EXCLUDED.algo_ver, frames=0, "
            "ftype=EXCLUDED.ftype, hashed_at=EXCLUDED.hashed_at, error=EXCLUDED.error",
            (media_id, source, algo_ver, ftype, db_now(), (error or '')[:500]))
        conn.commit()


def clear_hash_index():
    """Drop the whole fingerprint index (the "rehash everything" path)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM media.media_hash")
        cur.execute("DELETE FROM media.media_hash_meta")
        conn.commit()


def forget_hashes(media_ids):
    """Drop the fingerprints of specific rows so the next incremental scan
    computes them afresh — used when a row's thumbnail (the default hash
    source) has been re-rendered and the stored hashes describe the old
    image."""
    if not media_ids:
        return
    with _conn() as conn:
        cur = conn.cursor()
        ids = list(media_ids)
        cur.execute("DELETE FROM media.media_hash WHERE media_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM media.media_hash_meta WHERE media_id = ANY(%s)", (ids,))
        conn.commit()


# ── Hash index: reads ──────────────────────────────────────────────────────
def fetch_public_media():
    """(id, filepath, thumb, uploaded_by, upload_date, tags, ftype) for
    every public media row — the candidate set for a scan. Named tuples, so
    the type reads as `row.ftype`."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=namedtuple_row)
        cur.execute(
            "SELECT id, filepath, thumb, uploaded_by, upload_date, tags, ftype "
            "FROM media.media WHERE NOT is_private ORDER BY id")
        return cur.fetchall()


def count_public_media_by_type():
    """{ftype: count} over the public media rows."""
    with _conn() as conn:
        return dict(conn.execute(
            "SELECT ftype, count(*) FROM media.media WHERE NOT is_private "
            "GROUP BY ftype").fetchall())


def fetch_hashed_ids(source, algo_ver):
    """Ids already fingerprinted at this source and algorithm version —
    including ones recorded as errors, which must not be retried every
    run."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT media_id FROM media.media_hash_meta "
            "WHERE source=%s AND algo_ver=%s", (source, algo_ver))
        return {r[0] for r in cur.fetchall()}


def fetch_index(algo_ver):
    """The comparable fingerprint index.

    Returns (frames, meta):
      frames — [(media_id, phash, dhash, ahash)] one row per sampled frame,
               unsigned, ordered by media then frame index
      meta   — {media_id: (source, frames, width, height, bytes, ftype,
                           hashed_at)}

    Private media, and anything fingerprinted under a different algorithm
    version, is excluded.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT h.media_id, h.phash, h.dhash, h.ahash "
            "FROM media.media_hash h "
            "JOIN media.media_hash_meta mm ON mm.media_id = h.media_id "
            "JOIN media.media m ON m.id = h.media_id "
            "WHERE NOT m.is_private AND mm.algo_ver = %s AND mm.error IS NULL "
            "ORDER BY h.media_id, h.frame_idx", (algo_ver,))
        frames = [(mid, to_unsigned(p), to_unsigned(d), to_unsigned(a))
                  for mid, p, d, a in cur.fetchall()]
        cur.execute(
            "SELECT mm.media_id, mm.source, mm.frames, mm.width, mm.height, "
            "       mm.bytes, mm.ftype, mm.hashed_at "
            "FROM media.media_hash_meta mm "
            "JOIN media.media m ON m.id = mm.media_id "
            "WHERE NOT m.is_private AND mm.algo_ver = %s AND mm.error IS NULL",
            (algo_ver,))
        meta = {r[0]: tuple(r[1:]) for r in cur.fetchall()}
    return frames, meta


def coverage_stats(algo_ver):
    """Headline numbers for the page header: how much of the public library
    is fingerprinted, and when it was last touched."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM media.media WHERE NOT is_private")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FILTER (WHERE mm.error IS NULL), "
            "       COUNT(*) FILTER (WHERE mm.error IS NOT NULL), "
            "       MAX(mm.hashed_at) "
            "FROM media.media_hash_meta mm "
            "JOIN media.media m ON m.id = mm.media_id "
            "WHERE NOT m.is_private AND mm.algo_ver = %s", (algo_ver,))
        hashed, failed, last = cur.fetchone()
    return {
        'total': total,
        'hashed': hashed or 0,
        'failed': failed or 0,
        'pending': max(0, total - (hashed or 0) - (failed or 0)),
        'last_hashed_at': last,
    }


def fetch_hash_errors(algo_ver, limit=200):
    """Media that could not be fingerprinted, for the diagnostics table."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT mm.media_id, m.filepath, mm.ftype, mm.error, mm.hashed_at "
            "FROM media.media_hash_meta mm "
            "JOIN media.media m ON m.id = mm.media_id "
            "WHERE mm.error IS NOT NULL AND mm.algo_ver = %s AND NOT m.is_private "
            "ORDER BY mm.media_id LIMIT %s", (algo_ver, limit))
        return cur.fetchall()


# ── Dismissed pairs ────────────────────────────────────────────────────────
def _ordered(a, b):
    return (a, b) if a < b else (b, a)


def dismiss_pairs(pairs, user_id):
    """Mark (a, b) pairs as "not duplicates". Returns rows newly added."""
    rows = {_ordered(int(a), int(b)) for a, b in pairs if int(a) != int(b)}
    if not rows:
        return 0
    with _conn() as conn:
        cur = conn.cursor()
        added = 0
        for a, b in sorted(rows):
            cur.execute(
                "INSERT INTO media.dupe_dismissed (a_id, b_id, dismissed_by, dismissed_at) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (a_id, b_id) DO NOTHING",
                (a, b, user_id, db_now()))
            added += cur.rowcount
        conn.commit()
        return added


def undismiss_pairs(pairs):
    rows = {_ordered(int(a), int(b)) for a, b in pairs}
    if not rows:
        return 0
    with _conn() as conn:
        cur = conn.cursor()
        removed = 0
        for a, b in rows:
            cur.execute(
                "DELETE FROM media.dupe_dismissed WHERE a_id=%s AND b_id=%s",
                (a, b))
            removed += cur.rowcount
        conn.commit()
        return removed


def fetch_dismissed_pairs():
    """{(a_id, b_id)} with a_id < b_id — consulted by every grouping run."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT a_id, b_id FROM media.dupe_dismissed")
        return {(a, b) for a, b in cur.fetchall()}


def fetch_dismissed_detail(limit=500):
    """Dismissed pairs with both filenames, for the review table."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT d.a_id, d.b_id, ma.filepath, mb.filepath, "
            "       d.dismissed_at, u.username "
            "FROM media.dupe_dismissed d "
            "LEFT JOIN media.media ma ON ma.id = d.a_id "
            "LEFT JOIN media.media mb ON mb.id = d.b_id "
            "LEFT JOIN users.users u ON u.id = d.dismissed_by "
            "ORDER BY d.dismissed_at DESC NULLS LAST LIMIT %s", (limit,))
        return cur.fetchall()


# ── Quarantine (step one of the two-step resolve) ──────────────────────────
def add_quarantine(media_id, keeper_id, distance, user_id, tags_backup):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO media.dupe_quarantine "
            "(media_id, keeper_id, distance, marked_by, marked_at, tags_backup) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (media_id) DO UPDATE SET "
            "keeper_id=EXCLUDED.keeper_id, distance=EXCLUDED.distance, "
            "marked_by=EXCLUDED.marked_by, marked_at=EXCLUDED.marked_at",
            (media_id, keeper_id, distance, user_id, db_now(), tags_backup))
        conn.commit()
        return cur.rowcount > 0


def quarantined_ids():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT media_id FROM media.dupe_quarantine")
        return {r[0] for r in cur.fetchall()}


def fetch_quarantine():
    """Full review list: (media_id, filepath, thumb, tags, uploader,
    keeper_id, distance, marked_at, marked_by_name, bytes)."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT q.media_id, m.filepath, m.thumb, m.tags, "
            "       owner.username, q.keeper_id, q.distance, q.marked_at, "
            "       marker.username, mm.bytes "
            "FROM media.dupe_quarantine q "
            "JOIN media.media m ON m.id = q.media_id "
            "LEFT JOIN users.users owner ON owner.id = m.uploaded_by "
            "LEFT JOIN users.users marker ON marker.id = q.marked_by "
            "LEFT JOIN media.media_hash_meta mm ON mm.media_id = q.media_id "
            "ORDER BY q.marked_at DESC, q.media_id")
        return cur.fetchall()


def fetch_quarantine_rows(ids=None):
    """(media_id, tags_backup, keeper_id) for the given ids, or all."""
    with _conn() as conn:
        cur = conn.cursor()
        if ids is None:
            cur.execute(
                "SELECT media_id, tags_backup, keeper_id FROM media.dupe_quarantine")
        else:
            if not ids:
                return []
            cur.execute(
                "SELECT media_id, tags_backup, keeper_id FROM media.dupe_quarantine "
                "WHERE media_id = ANY(%s)", (list(ids),))
        return cur.fetchall()


def remove_quarantine(ids):
    if not ids:
        return 0
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM media.dupe_quarantine WHERE media_id = ANY(%s)",
                    (list(ids),))
        conn.commit()
        return cur.rowcount


def quarantine_summary():
    """(count, total bytes) for the purge confirmation."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(mm.bytes), 0) "
            "FROM media.dupe_quarantine q "
            "LEFT JOIN media.media_hash_meta mm ON mm.media_id = q.media_id")
        return cur.fetchone()


# ── Scan job ───────────────────────────────────────────────────────────────
_JOB_COLUMNS = (
    "id, state, params, total, processed, hashed, skipped, failed, "
    "cancel_requested, error, started_by, started_at, heartbeat, finished_at"
)


def _job_dict(row):
    if not row:
        return None
    return dict(zip(
        ('id', 'state', 'params', 'total', 'processed', 'hashed', 'skipped',
         'failed', 'cancel_requested', 'error', 'started_by', 'started_at',
         'heartbeat', 'finished_at'), row))


def claim_job(params, total, user_id):
    """Insert a running job unless one is already running.

    Returns the job id, or None when another worker holds the slot. The
    WHERE NOT EXISTS guard covers the common case without raising; the
    partial unique index is the hard backstop for two workers inserting at
    the same instant, and its violation is caught here so the pooled
    connection is never handed back mid-abort.
    """
    from psycopg import errors
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO media.dupe_job "
                "(state, params, total, started_by, started_at, heartbeat) "
                "SELECT 'running', %s, %s, %s, %s, %s "
                "WHERE NOT EXISTS (SELECT 1 FROM media.dupe_job WHERE state='running') "
                "RETURNING id",
                (params, total, user_id, db_now(), db_now()))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
    except errors.UniqueViolation:
        return None


def update_job_progress(job_id, processed, hashed, skipped, failed):
    """Write counters and heartbeat; returns True while the job should keep
    running (i.e. no cancel was requested and the row is still 'running')."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.dupe_job SET processed=%s, hashed=%s, skipped=%s, "
            "failed=%s, heartbeat=%s WHERE id=%s "
            "RETURNING cancel_requested, state",
            (processed, hashed, skipped, failed, db_now(), job_id))
        row = cur.fetchone()
        conn.commit()
    return bool(row) and not row[0] and row[1] == 'running'


def finish_job(job_id, state, error=None):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.dupe_job SET state=%s, error=%s, finished_at=%s, "
            "heartbeat=%s WHERE id=%s",
            (state, (error or None) and str(error)[:500], db_now(), db_now(),
             job_id))
        conn.commit()
        return cur.rowcount > 0


def request_cancel():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.dupe_job SET cancel_requested=true WHERE state='running'")
        conn.commit()
        return cur.rowcount > 0


def finish_unresponsive_job(max_age, state, error):
    """Close out a running job whose worker has not heartbeated recently.

    The escape hatch behind Cancel: if the worker thread is gone (crashed
    process, killed container) the cancel flag will never be read, so the
    request itself ends the job rather than leaving the slot held.
    """
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.dupe_job SET state=%s, error=%s, finished_at=%s "
            "WHERE state='running' AND (heartbeat IS NULL OR heartbeat < %s)",
            (state, error, db_now(), db_now() - max_age))
        conn.commit()
        return cur.rowcount


def fetch_running_job():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM media.dupe_job WHERE state='running' LIMIT 1")
        return _job_dict(cur.fetchone())


def fetch_last_job():
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM media.dupe_job ORDER BY id DESC LIMIT 1")
        return _job_dict(cur.fetchone())


def reap_stale_jobs(max_age):
    """Fail jobs whose worker stopped heartbeating (its process died), so
    the single-runner slot is not held forever. Returns rows reaped."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE media.dupe_job SET state='error', "
            "error='scan worker stopped responding', finished_at=%s "
            "WHERE state='running' AND (heartbeat IS NULL OR heartbeat < %s)",
            (db_now(), db_now() - max_age))
        conn.commit()
        if cur.rowcount:
            _log.warning("dupes: reaped %s stale scan job(s)", cur.rowcount)
        return cur.rowcount
