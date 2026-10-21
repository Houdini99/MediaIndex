"""File-type detection, hashing, and thumbnail generation.

Pure I/O helpers — no Flask, no SQL.
"""
import hashlib
import io
import logging
import os
import re
import time

from PIL import Image, ImageOps, ImageSequence

from ..config import Config
from .crypto import ENC_SUFFIX, NONCE_BYTES, enc_name, iter_decrypted, strip_enc


def allowed_file(filename):
    ext = filename.rsplit('.', 1)[-1].lower()
    return ext in (Config.ALLOWED_IMAGE_EXT | Config.ALLOWED_GIF_EXT | Config.ALLOWED_VIDEO_EXT)


def file_type(filename):
    ext = filename.rsplit('.', 1)[-1].lower()
    if ext in Config.ALLOWED_IMAGE_EXT:
        return "image"
    if ext in Config.ALLOWED_GIF_EXT:
        return "gif"
    if ext in Config.ALLOWED_VIDEO_EXT:
        return "video"
    return "unknown"


#: Read size for hashing and copying media. 8 KiB made a multi-GB video
#: cost hundreds of thousands of Python-level loop turns; 1 MiB is past the
#: point where hashlib, not the loop, is the bottleneck.
IO_CHUNK = 1024 * 1024


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(IO_CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def save_stream_hashed(stream, path):
    """Write `stream` to `path` and return its SHA-256, in one pass.

    Hashing the bytes on their way to disk saves reading a large video
    back in full right after writing it.
    """
    h = hashlib.sha256()
    with open(path, 'wb') as out:
        for chunk in iter(lambda: stream.read(IO_CHUNK), b''):
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


#: Modes a PNG can hold as-is. Anything else (CMYK and YCbCr JPEGs, I;16
#: scans, …) made `save(format='PNG')` raise, which the except below turned
#: into a placeholder thumbnail for a perfectly readable image.
_PNG_MODES = {'1', 'L', 'LA', 'P', 'RGB', 'RGBA'}


def thumbnail_image(img):
    """Upright, PNG-storable, THUMB_SIZE-bounded version of `img`."""
    # Shrink first, into a square box so the rotation below cannot push it
    # out of bounds: on a JPEG, thumbnail() decodes at reduced scale (draft
    # mode), whereas transposing first would force a full-resolution decode
    # of a 24 MP photo only to throw most of it away.
    side = max(Config.THUMB_SIZE)
    img.thumbnail((side, side))
    # Phone cameras store pixels in sensor order and record the rotation in
    # the EXIF Orientation tag. Browsers honour the tag when showing the
    # original, but the PNG written here carries no EXIF — so without this
    # the thumbnail of every such photo came out sideways or upside down.
    img = ImageOps.exif_transpose(img)
    if img.mode not in _PNG_MODES:
        img = img.convert('RGBA' if 'A' in img.getbands() else 'RGB')
    img.thumbnail(Config.THUMB_SIZE)
    return img


#: `<base>.png` or `<base>.v<N>.png` — see versioned_thumb_name().
_THUMB_NAME = re.compile(r'^(?P<base>.+?)(?:\.v(?P<n>\d+))?\.png$')


def versioned_thumb_name(name):
    """The next name for a thumbnail whose image is being replaced.

    `/thumbs/<name>` is served `immutable` for a year (see
    app._apply_cache_policy), which is only sound while a name never changes
    its bytes. So a replacement thumbnail — the real frame of a video that
    started out with a placeholder, or an image re-rendered upright — gets a
    new name and the row is repointed at it, instead of being rewritten in
    place under a URL browsers may already have cached for good.

    `<hash>.png` → `<hash>.v2.png` → `<hash>.v3.png`; an `.enc` suffix is
    kept. Idempotent in shape: always exactly one `.v<N>` component.
    """
    base_name = os.path.basename(name)
    private = base_name != strip_enc(base_name)
    plain = strip_enc(base_name)
    m = _THUMB_NAME.match(plain)
    if m:
        base, n = m['base'], int(m['n'] or 1)
    else:                                   # not a .png name at all
        base, n = os.path.splitext(plain)[0], 1
    new = f"{base}.v{n + 1}.png"
    return enc_name(new) if private else new


def gen_image_thumb(src, dst):
    try:
        with Image.open(src) as img:
            if img.format == 'GIF':
                frame = next(ImageSequence.Iterator(img)).copy()
                thumbnail_image(frame).save(dst, format='PNG')
            else:
                thumbnail_image(img).save(dst, format='PNG')
        return dst
    except Exception:
        return gen_placeholder_thumb(dst)


def gen_video_thumb(src, dst, timeout=None):
    """Render a video's thumbnail from an early frame.

    Tries 2 s in, then the middle, 1 s and a third of the way through, and
    finally the very first frame, keeping the first that decodes. The whole
    render is bounded by `timeout` (default Config.VIDEO_THUMB_TIMEOUT)
    because ffmpeg on a malformed file can block forever, and both callers —
    the upload thumbnail worker and the WebDAV staging scanner — are single
    shared threads that would otherwise be wedged for every user.

    Always leaves a file at `dst`: the placeholder when nothing decodes in
    time, since the caller has already pointed a row at this name.
    """
    from . import ffmpeg
    if timeout is None:
        timeout = Config.VIDEO_THUMB_TIMEOUT
    deadline = time.monotonic() + timeout

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f'no frame decoded within {timeout:.0f}s')
        return remaining

    try:
        dur = ffmpeg.duration(src, left())
        candidates = [2, 1] if dur is None else [2, dur / 2, 1, dur / 3]
        candidates = [t for t in candidates if dur is None or t <= dur] + [0]
        for t in candidates:
            try:
                frame = ffmpeg.frame_at(src, t, left())
                break
            except (ValueError, RuntimeError):
                continue                # nothing at t — try the next one
        else:
            raise ValueError('no decodable frame')
        frame.thumbnail(Config.THUMB_SIZE)
        frame.save(dst, format='PNG')
        return dst
    except Exception:
        logging.warning("thumbnail: video render failed for %s", src,
                        exc_info=True)
        return gen_placeholder_thumb(dst)


#: The placeholder's fill — and how a placeholder is recognised later.
PLACEHOLDER_COLOR = (32, 48, 32)

#: A placeholder is a solid-colour PNG of a few hundred bytes. Any stored
#: thumbnail bigger than this is a real render and is never even opened.
PLACEHOLDER_MAX_BYTES = 4096


def gen_placeholder_thumb(dst):
    img = Image.new('RGB', Config.THUMB_SIZE, PLACEHOLDER_COLOR)
    img.save(dst, format='PNG')
    return dst


def is_placeholder_thumb(path, key=None):
    """True when the stored thumbnail at `path` is gen_placeholder_thumb's
    output: the upload's real render never happened (thumbnail job lost to
    a restart) or failed (undecodable file).

    Decided by content, not name — the placeholder is written under the
    same name the real thumbnail would have had. `key` decrypts a private
    (`.enc`) thumbnail; without one a private file never counts.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    private = path.endswith(ENC_SUFFIX)
    if size > PLACEHOLDER_MAX_BYTES + (NONCE_BYTES if private else 0):
        return False
    try:
        if private:
            if key is None:
                return False
            img = Image.open(io.BytesIO(b''.join(iter_decrypted(path, key))))
        else:
            img = Image.open(path)
        with img:
            w, h = Config.THUMB_SIZE
            return (img.size == (w, h)
                    and img.convert('RGB').getcolors(1) == [(w * h, PLACEHOLDER_COLOR)])
    except Exception:
        return False
