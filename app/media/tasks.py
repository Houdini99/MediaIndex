"""Background thumbnail generation.

Video thumbnails (ffmpeg) are too slow to run inside the upload
request, so the upload route writes a placeholder thumb, inserts the DB row
pointing at it, and enqueues a job here. The worker renders the real frame
under the *next* thumbnail name (`<hash>.v2.png`), repoints the row at it,
and deletes the placeholder.

Overwriting the placeholder in place would keep its name, but
/thumbs/<name> is served `immutable` for a year, so anyone who opened the
gallery during the few seconds before the worker finished would keep the
placeholder in their browser cache for good. A new name is a new URL.

For private uploads the media file on disk is already encrypted, so the job
carries the owner's key: the worker decrypts to a temp file, renders the
thumb, encrypts it to `<name>.enc`, and cleans up all plaintext.

A stdlib queue + one daemon thread is plenty at this scale; jobs lost on
restart simply leave a placeholder thumb behind.
"""
import logging
import os
import queue
import threading

from ..config import Config
from .crypto import encrypt_file, plaintext_copy, strip_enc
from .db import replace_thumb

_queue = queue.Queue()
_started = threading.Event()


def enqueue_video_thumb(src_path, thumb_dst, key=None, notify_event=None):
    """Queue real-thumbnail generation for a stored video.

    `src_path`/`thumb_dst` are the final on-disk paths ('.enc' suffixed when
    private); `key` is the owner's encryption key for private files.
    `notify_event` is an already-built Discord event to publish once the
    real thumb exists (public videos only).
    """
    start_worker()
    _queue.put((src_path, thumb_dst, key, notify_event))


def start_worker():
    if _started.is_set():
        return
    _started.set()
    threading.Thread(target=_worker_loop, name='thumb-worker', daemon=True).start()


def _worker_loop():
    while True:
        job = _queue.get()
        try:
            _run_job(*job)
        finally:
            _queue.task_done()


def _run_job(src_path, thumb_dst, key, notify_event):
    try:
        new_thumb = _process(src_path, thumb_dst, key)
        if new_thumb and notify_event:
            # The bot attaches the thumbnail from disk by this name, and the
            # placeholder the event was built with is gone now.
            notify_event['thumb'] = new_thumb
    except Exception:
        logging.exception("thumbnail worker: job failed for %s", src_path)
    finally:
        if notify_event:
            # Announce even if the render failed — the media row exists
            # either way; the placeholder thumb is attached instead.
            from ..notifications import publish_event
            publish_event(notify_event)


def _process(src_path, thumb_dst, key):
    """Render the real thumbnail and swap the row over to it.

    `thumb_dst` is the placeholder the row points at now. Returns the new
    thumbnail's basename, or None when there was nothing to swap (source
    gone, or the row changed underneath — deleted, or its privacy flipped,
    which renames the placeholder).
    """
    if not os.path.exists(src_path):
        logging.warning("thumbnail worker: source vanished: %s", src_path)
        return None

    from .processing import is_placeholder_thumb, versioned_thumb_name

    new_dst = os.path.join(os.path.dirname(thumb_dst),
                           versioned_thumb_name(thumb_dst))
    try:
        _render(src_path, new_dst, key)
        if is_placeholder_thumb(new_dst, key):
            # No frame could be decoded, so the render fell back to the
            # placeholder the row already shows. Swapping one placeholder
            # for an identical one under a new name would only churn URLs
            # (and the admin integrity page's re-render would do it on every
            # click), so keep the row as it is.
            os.remove(new_dst)
            return None
        swapped = replace_thumb(thumb_dst, new_dst)
    except BaseException:
        if os.path.exists(new_dst):
            os.remove(new_dst)
        raise
    if not swapped:
        os.remove(new_dst)
        return None
    if os.path.exists(thumb_dst):
        os.remove(thumb_dst)
    return os.path.basename(new_dst)


def _render(src_path, thumb_dst, key):
    from .processing import gen_video_thumb

    if key is None:
        gen_video_thumb(src_path, thumb_dst)
        return

    # Private file: decrypt to temp, render, re-encrypt the thumb. Both the
    # whole-file decrypt and the rendered plaintext thumb are written to the
    # non-served scratch dir — never into UPLOAD_FOLDER/THUMB_FOLDER, where a
    # request could reach them before they are cleaned up.
    plain_name = strip_enc(os.path.basename(src_path))
    tmp_thumb = os.path.join(Config.tmp_dir(), f"thumb-out-{plain_name}.png")
    try:
        with plaintext_copy(src_path, key, f"thumb-src-{plain_name}") as tmp_src:
            gen_video_thumb(tmp_src, tmp_thumb)
        encrypt_file(tmp_thumb, thumb_dst, key)
    finally:
        if os.path.exists(tmp_thumb):
            os.remove(tmp_thumb)
