"""Background scanner for the WebDAV staging inbox.

Discovers files dumped into staging/<user_id>/ via WebDAV, waits until they
are size/mtime-stable (DAV clients write in place), then hashes them for
early duplicate detection and pre-renders a queue thumbnail. It also
reconciles rows whose files were deleted or renamed through WebDAV.

Exactly one scanner runs across all gunicorn workers: start_scanner() only
starts the thread in the worker that wins a non-blocking flock on a file in
DATA_DIR. If that worker dies, gunicorn respawns it, the kernel drops the
lock, and the replacement worker re-acquires it.
"""
import fcntl
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from ..config import Config
from .db import hash_exists
from .processing import (
    allowed_file, file_type, gen_image_thumb, gen_video_thumb, sha256_file,
)
from . import staging_db

# Names DAV clients and OSes litter staging with — never queue these.
_JUNK_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
_JUNK_SUFFIXES = ('.part', '.tmp', '.crdownload', '.partial')

# Claimed rows older than this are considered leftovers of a crashed
# promote request and are released by reconciliation.
_STALE_PROMOTE = timedelta(hours=1)

_lock_fh = None          # held for process lifetime — do not close
_started = threading.Event()


def start_scanner():
    """Start the scanner thread if this process wins the cross-worker lock."""
    global _lock_fh
    if _started.is_set():
        return
    lock_path = os.path.join(Config.DATA_DIR, 'staging_scanner.lock')
    fh = open(lock_path, 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return
    _lock_fh = fh
    _started.set()
    threading.Thread(target=_loop, name='staging-scanner', daemon=True).start()


def _loop():
    while True:
        try:
            scan_once()
        except Exception:
            logging.exception("staging scanner: scan cycle failed")
        time.sleep(Config.STAGING_SCAN_INTERVAL)


def _is_junk(name):
    return (
        name.startswith('.')
        or name in _JUNK_NAMES
        or name.lower().endswith(_JUNK_SUFFIXES)
    )


def _walk_user_files(user_dir):
    """Yield fnames (relative to user_dir) of every non-junk file."""
    for root, dirs, files in os.walk(user_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in files:
            if _is_junk(name):
                continue
            yield os.path.relpath(os.path.join(root, name), user_dir)


def scan_once():
    """One reconcile-and-register pass. Called by the scanner thread, and
    directly from tests."""
    rows = staging_db.all_rows_for_reconcile()
    known = _reconcile(rows)
    _register_and_process(known)


def _reconcile(rows):
    """Drop rows whose files vanished; release crashed promote claims.
    Returns the surviving rows as {(user_id, fname): (sid, size, mtime, status)}."""
    known = {}
    for sid, user_id, fname, size, mtime, status, thumb, claimed_at in rows:
        path = staging_db.staging_file_path(user_id, fname)
        if status == 'promoting':
            # Leave live claims alone; release ones whose request crashed.
            # claimed_at is an aware datetime (timestamptz) or None.
            if claimed_at is None or datetime.now(timezone.utc) - claimed_at > _STALE_PROMOTE:
                if os.path.isfile(path):
                    staging_db.release_claim(sid)
                else:
                    _drop_row(sid, thumb)
                    continue
            known[(user_id, fname)] = (sid, size, mtime, status)
            continue
        if not os.path.isfile(path):
            # Deleted or renamed via DAV (a rename shows up as delete+add).
            _drop_row(sid, thumb)
            continue
        known[(user_id, fname)] = (sid, size, mtime, status)
    return known


def _drop_row(sid, thumb):
    staging_db.remove_staging_thumb(thumb)
    staging_db.delete_staging_row(sid)


def _register_and_process(known):
    try:
        entries = os.listdir(Config.STAGING_DIR)
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        user_id = int(entry)
        user_dir = os.path.join(Config.STAGING_DIR, entry)
        for fname in _walk_user_files(user_dir):
            path = os.path.join(user_dir, fname)
            try:
                st = os.stat(path)
            except OSError:
                continue  # vanished mid-scan; next cycle reconciles
            size, mtime = st.st_size, st.st_mtime
            row = known.get((user_id, fname))
            if row is None:
                sid = staging_db.insert_staging_row(user_id, fname, size, mtime)
                # Old files (e.g. dumped while the app was down) skip the
                # stabilization wait via the mtime-age fallback.
                if sid and time.time() - mtime > Config.STAGING_STABLE_AGE:
                    _process_file(sid, path)
                continue
            sid, known_size, known_mtime, status = row
            changed = size != known_size or mtime != known_mtime
            if status == 'pending':
                if changed:
                    staging_db.update_staging_size(sid, size, mtime)
                else:
                    # Unchanged since the last cycle — stable, process it.
                    _process_file(sid, path)
            elif status in ('ready', 'duplicate', 'unsupported', 'error') and changed:
                # Overwritten via DAV — back through the pipeline.
                staging_db.update_staging_size(sid, size, mtime)


def _process_file(sid, path):
    base = os.path.basename(path)
    if not allowed_file(base):
        staging_db.update_staging_processed(sid, None, None, 'unknown', 'unsupported')
        return
    try:
        fh = sha256_file(path)
        thumb_name = f"stg_{sid}.png"
        thumb_path = os.path.join(Config.STAGING_THUMB_DIR, thumb_name)
        ftype = file_type(base)
        if ftype == 'video':
            # Deadline-bounded (VIDEO_THUMB_TIMEOUT): this is the single
            # scanner thread, and an ffmpeg that never returns on one
            # malformed upload would stop reconciliation and queue updates
            # for every user until the container restarts.
            gen_video_thumb(path, thumb_path)
        else:
            gen_image_thumb(path, thumb_path)
        status = 'duplicate' if hash_exists(fh) else 'ready'
        staging_db.update_staging_processed(sid, fh, thumb_name, ftype, status)
    except Exception as e:
        # Mark the row instead of leaving it 'pending', which would retry
        # (and log a traceback) every scan cycle forever. Re-uploading the
        # file via DAV resets it to 'pending' through the changed-size path.
        logging.exception("staging scanner: failed to process %s", path)
        staging_db.set_staging_error(sid, str(e))
