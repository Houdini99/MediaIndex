"""Re-render image thumbnails that are wrong on disk.

Two callers:

* scripts/regen_image_thumbs.py → repair_rotated_thumbs(), for photos a
  phone stored "sideways with a rotate flag": their thumbnails must honour
  the EXIF Orientation tag, as gen_image_thumb() does, and this re-renders
  any on disk that don't.
* The admin integrity page → rerender_image_thumb(), for an image whose
  thumbnail is still the placeholder.

A re-rendered thumbnail gets a new name (versioned_thumb_name) and the row
is repointed at it, then the old file is deleted. Rewriting `<hash>.png` in
place would reach nobody: /thumbs/ is served `immutable` for a year, so a
browser that had already seen the old image would keep it.

The duplicate finder hashes thumbnails by default, so a re-rendered row's
fingerprints are dropped too; the next incremental scan recomputes them.
"""
import logging
import os
from contextlib import contextmanager

from PIL import Image

from ..auth.db import get_user_enc_key
from ..config import Config
from .crypto import encrypt_file, plaintext_copy, strip_enc
from .db import get_image_rows, media_path, replace_thumb, thumb_path
from .dupes_db import forget_hashes
from .processing import thumbnail_image, versioned_thumb_name

#: EXIF tag id of Orientation. 1 (or absent) means "stored upright".
_ORIENTATION = 0x0112


@contextmanager
def _plaintext(src, key, tag):
    """Path to `src` in plaintext: `src` itself, or — for a private file,
    `key` given — a decrypted copy in the non-served scratch dir, removed
    afterwards like every other plaintext this app makes of a private file."""
    if key is None:
        yield src
        return
    with plaintext_copy(
            src, key, f"rethumb-{tag}-{strip_enc(os.path.basename(src))}") as tmp:
        yield tmp


def _orientation(path):
    with Image.open(path) as img:
        return img.getexif().get(_ORIENTATION, 1) or 1


def _render(src, dst):
    """Write the thumbnail of `src` to `dst`, raising on failure — unlike
    gen_image_thumb, which would quietly write a placeholder and so replace
    a merely wrong thumbnail with a blank one."""
    with Image.open(src) as img:
        thumbnail_image(img).save(dst, format='PNG')


def _swap_in(plain_src, old, key, tag):
    """Render `plain_src` under the next name after `old` and repoint the
    row at it. Returns (new_name, None) or (None, reason)."""
    new = versioned_thumb_name(old)
    new_abs = thumb_path(new)
    tmp_thumb = os.path.join(Config.tmp_dir(), f"rethumb-{tag}.png")
    try:
        if key is not None:
            _render(plain_src, tmp_thumb)
            encrypt_file(tmp_thumb, new_abs, key)
        else:
            _render(plain_src, new_abs)
    except Exception as exc:
        if os.path.exists(new_abs):
            os.remove(new_abs)
        return None, f'render failed: {type(exc).__name__}: {exc}'
    finally:
        if os.path.exists(tmp_thumb):
            os.remove(tmp_thumb)
    if not replace_thumb(old, new):
        # Deleted, or privacy flipped (which renames the thumbnail), while
        # this ran — leave that row to its new state.
        os.remove(new_abs)
        return None, 'row changed meanwhile'
    old_abs = thumb_path(old)
    if os.path.exists(old_abs):
        os.remove(old_abs)
    return new, None


def rerender_image_thumb(mid, filepath, thumb, key):
    """Re-render one image row's thumbnail under a new name.

    `key` is the owner's key for a private row, else None. Returns
    (new_name, None) on success or (None, reason).
    """
    src = media_path(filepath)
    if not os.path.isfile(src):
        return None, 'media file missing'
    with _plaintext(src, key, mid) as plain:
        new, reason = _swap_in(plain, os.path.basename(thumb), key, mid)
    if new:
        forget_hashes([mid])
    return new, reason


def repair_rotated_thumbs(dry_run=False, everything=False):
    """Re-render the thumbnail of every image row whose original carries a
    rotating EXIF orientation (or of every image row, with `everything`).

    Returns (repaired, skipped) where `repaired` is a list of
    (media_id, old_thumb, new_thumb, orientation) and `skipped` a list of
    (media_id, reason). A dry run reports what would be repaired without
    writing anything.
    """
    repaired, skipped = [], []
    for mid, filepath, thumb, owner, is_private in get_image_rows():
        src = media_path(filepath)
        if not os.path.isfile(src):
            skipped.append((mid, 'media file missing'))
            continue
        if not thumb:
            skipped.append((mid, 'row has no thumbnail'))
            continue
        key = None
        if is_private:
            key = get_user_enc_key(owner)
            if not key:
                skipped.append((mid, "owner's key missing"))
                continue
        old = os.path.basename(thumb)
        with _plaintext(src, key, mid) as plain:
            try:
                orientation = _orientation(plain)
            except Exception as exc:
                skipped.append((mid, f'unreadable: {type(exc).__name__}'))
                continue
            if orientation == 1 and not everything:
                continue
            if dry_run:
                repaired.append((mid, old, versioned_thumb_name(old), orientation))
                continue
            new, reason = _swap_in(plain, old, key, mid)
        if not new:
            skipped.append((mid, reason))
            continue
        forget_hashes([mid])
        repaired.append((mid, old, new, orientation))
    if repaired and not dry_run:
        logging.info("thumb_repair: re-rendered %d image thumbnail(s)",
                     len(repaired))
    return repaired, skipped
