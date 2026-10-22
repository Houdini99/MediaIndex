"""Filesystem/DB integrity checks for the admin panel.

Media rows store bare basenames (insert_media writes them that way),
and all comparisons here are done on basenames against the configured
folders — so the checks work no matter where the project root is mounted.
"""
import os
import time

from ..auth.db import get_user_enc_key
from ..config import Config
from ..media.crypto import ENC_SUFFIX, NONCE_BYTES, strip_enc
from ..media.db import (
    delete_media_rows, get_all_media_paths, get_privacy_for_file, media_path,
    thumb_path,
)
from ..media.processing import (
    PLACEHOLDER_MAX_BYTES, file_type, is_placeholder_thumb,
)

#: Files younger than this are never reported (or cleaned up) as orphans.
#: An upload is written into UPLOAD_FOLDER under a temp name first, and a
#: finished one exists on disk for a moment before its row is inserted, so
#: for a while every healthy in-flight upload *looks* orphaned, and "delete
#: orphaned media files" would delete it out from under the request.
ORPHAN_GRACE = 3600


def _listdir_files(folder):
    """{name: (size, mtime)} for the regular files directly in `folder`."""
    try:
        files = {}
        for e in os.scandir(folder):
            if e.is_file():
                st = e.stat()
                files[e.name] = (st.st_size, st.st_mtime)
        return files
    except FileNotFoundError:
        return {}


def folder_stats(folder):
    """Return (file_count, total_bytes) for a flat folder."""
    files = _listdir_files(folder)
    return len(files), sum(size for size, _ in files.values())


def scan():
    """Compare the media table against the upload/thumb folders.

    Returns a dict with:
      missing_rows  — [(id, filepath, thumb)] rows whose media file is gone
      orphan_files  — [(name, size)] files on disk no row references
      orphan_thumbs — [(name, size)] thumbnails no row references
      placeholder_thumbs — [(id, filepath, thumb)] rows still showing the
                      placeholder instead of a real thumbnail
    """
    rows = get_all_media_paths()
    media_refs = {os.path.basename(fp) for _, fp, _ in rows if fp}
    thumb_refs = {os.path.basename(th) for _, _, th in rows if th}

    disk_media = _listdir_files(Config.UPLOAD_FOLDER)
    disk_thumbs = _listdir_files(Config.THUMB_FOLDER)

    cutoff = time.time() - ORPHAN_GRACE
    missing_rows = [
        (mid, fp, th) for mid, fp, th in rows
        if fp and os.path.basename(fp) not in disk_media
    ]
    orphan_files = sorted(
        (name, size) for name, (size, mtime) in disk_media.items()
        if name not in media_refs and mtime < cutoff
    )
    orphan_thumbs = sorted(
        (name, size) for name, (size, mtime) in disk_thumbs.items()
        if name not in thumb_refs and mtime < cutoff
    )
    placeholder_thumbs = [
        (mid, fp, th) for mid, fp, th in rows
        if th and _is_placeholder(os.path.basename(th), disk_thumbs)
    ]
    return {
        'missing_rows': missing_rows,
        'orphan_files': orphan_files,
        'orphan_thumbs': orphan_thumbs,
        'placeholder_thumbs': placeholder_thumbs,
    }


def _owner_key(thumb_name):
    """The owner's key for a private (`.enc`) thumbnail, else None."""
    if not thumb_name.endswith(ENC_SUFFIX):
        return None
    row = get_privacy_for_file(thumb_name)
    return get_user_enc_key(row[0]) if row else None


def _is_placeholder(name, disk_thumbs):
    entry = disk_thumbs.get(name)
    # Sized from the listing we already have: only the handful of tiny
    # files are opened, and only a tiny private one costs a key lookup.
    if not entry or entry[0] > PLACEHOLDER_MAX_BYTES + NONCE_BYTES:
        return False
    return is_placeholder_thumb(os.path.join(Config.THUMB_FOLDER, name),
                                _owner_key(name))


def cleanup_missing_rows():
    """Drop media rows whose file is gone; also remove their now-orphaned
    thumbnails. Re-scans first so only currently-missing rows are touched."""
    missing = scan()['missing_rows']
    for _, _, th in missing:
        if th:
            thumb_path = os.path.join(Config.THUMB_FOLDER, os.path.basename(th))
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
    return delete_media_rows([mid for mid, _, _ in missing])


def rerender_placeholder_thumbs():
    """Give every placeholder thumbnail another render.

    A placeholder is left behind when the upload's thumbnail job was lost
    (the queue is in memory; a restart drops it) or when the file could not
    be decoded. Videos go back to the background worker — ffmpeg is slow,
    and the worker kills a hung decode — and images are re-rendered here.
    A file that still cannot be decoded simply keeps its placeholder.

    Returns (queued_videos, rerendered_images, failed).
    """
    from ..media.tasks import enqueue_video_thumb
    from ..media.thumb_repair import rerender_image_thumb

    queued = rerendered = failed = 0
    for mid, fp, th in scan()['placeholder_thumbs']:
        name = os.path.basename(th)
        key = _owner_key(name)
        if name.endswith(ENC_SUFFIX) and key is None:
            failed += 1
            continue
        if file_type(strip_enc(os.path.basename(fp))) == 'video':
            enqueue_video_thumb(media_path(fp), thumb_path(th), key)
            queued += 1
        elif rerender_image_thumb(mid, fp, th, key)[0]:
            rerendered += 1
        else:
            failed += 1
    return queued, rerendered, failed


def cleanup_orphan_files():
    removed = 0
    for name, _ in scan()['orphan_files']:
        path = os.path.join(Config.UPLOAD_FOLDER, name)
        if os.path.exists(path):
            os.remove(path)
            removed += 1
    return removed


def cleanup_orphan_thumbs():
    removed = 0
    for name, _ in scan()['orphan_thumbs']:
        path = os.path.join(Config.THUMB_FOLDER, name)
        if os.path.exists(path):
            os.remove(path)
            removed += 1
    return removed
