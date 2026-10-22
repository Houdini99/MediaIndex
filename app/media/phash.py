"""Perceptual hashing for the admin near-duplicate finder.

Three 64-bit hashes are computed per sampled frame:

  aHash — 8x8 grayscale compared against the mean. Cheap, weak.
  dHash — 9x8 grayscale, sign of the horizontal gradient. Good on crops.
  pHash — 32x32 grayscale, 2-D DCT-II, low-frequency 8x8 block compared
          against its median. The primary metric: it survives rescaling,
          re-compression and mild colour shifts, which is exactly what a
          re-encoded re-upload looks like.

Every media item is fingerprinted as a *sequence* of frame hashes:
length 1 for an image or GIF, N for a video. That unifies the comparison
path — an image is a one-frame video — so a screenshot of an indexed clip
is found by the same code that finds two re-encodes of that clip.

Pure I/O and arithmetic: no Flask, no SQL, mirroring processing.py.
"""

import numpy as np
from PIL import Image, ImageSequence

# Bump when a hash definition changes: stored hashes carry the version they
# were computed with, and a scan re-hashes anything computed under an older
# one instead of comparing incompatible fingerprints.
#
# 2: video frames come straight from ffmpeg (`-ss t`, the first frame at or
#    after t). Version 1 picked frames by index arithmetic: usually the
#    same frame, sometimes its neighbour — close enough to match, but not
#    a difference worth letting two fingerprints of one clip carry.
ALGO_VER = 2

HASH_BITS = 64
_MASK64 = (1 << 64) - 1
_PHASH_IMG = 32          # DCT input size
_PHASH_BLOCK = 8         # low-frequency block kept (8x8 = 64 bits)

# Default number of frames sampled from a video.
DEFAULT_VIDEO_FRAMES = 5


# ── Bit helpers ────────────────────────────────────────────────────────────
def _bits_to_int(bits):
    """Pack a boolean array (row-major, 64 entries) into an int."""
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bool(bit))
    return value


def hamming(a, b):
    """Hamming distance between two 64-bit hashes."""
    return ((a ^ b) & _MASK64).bit_count()


# ── The DCT-II basis, built once ───────────────────────────────────────────
def _dct_matrix(n):
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    m[0] *= np.sqrt(0.5)
    return m * np.sqrt(2.0 / n)


_DCT = _dct_matrix(_PHASH_IMG)


# ── The three hashes ───────────────────────────────────────────────────────
def _gray(img, size):
    """Grayscale ndarray of `img` resized to `size` (w, h)."""
    return np.asarray(
        img.convert('L').resize(size, Image.Resampling.LANCZOS),
        dtype=np.float64,
    )


def ahash64(img):
    px = _gray(img, (8, 8))
    return _bits_to_int(px > px.mean())


def dhash64(img):
    px = _gray(img, (9, 8))
    return _bits_to_int(px[:, 1:] > px[:, :-1])


def phash64(img):
    px = _gray(img, (_PHASH_IMG, _PHASH_IMG))
    coeffs = _DCT @ px @ _DCT.T
    block = coeffs[:_PHASH_BLOCK, :_PHASH_BLOCK]
    return _bits_to_int(block > np.median(block))


def hash_frame(img):
    """(phash, dhash, ahash) for one PIL image."""
    return phash64(img), dhash64(img), ahash64(img)


# ── Source readers ─────────────────────────────────────────────────────────
def hash_image_file(path):
    """Fingerprint a still image, GIF or PNG thumbnail.

    Returns (frames, width, height) where `frames` is a one-element list of
    (phash, dhash, ahash). Animated GIFs are represented by their first
    frame — the same frame the thumbnail pipeline picks, so a GIF and its
    thumbnail fingerprint alike.
    """
    with Image.open(path) as img:
        width, height = img.size
        if getattr(img, 'is_animated', False):
            frame = next(ImageSequence.Iterator(img)).convert('RGB')
        else:
            frame = img.convert('RGB')
        return [hash_frame(frame)], width, height


def hash_video_frames(path, count=DEFAULT_VIDEO_FRAMES, timeout=180.0,
                      keepalive=None):
    """Fingerprint a video by sampling `count` evenly spaced frames.

    Samples at duration*(i+1)/(count+1) so the black lead-in and the
    trailing frame — the two least distinctive parts of most clips — are
    skipped. Frames that fail to decode are dropped rather than aborting;
    a clip that yields nothing raises, and the caller records the failure.

    The whole item is bounded by `timeout` seconds, and `keepalive` (see
    app.media.ffmpeg.run) can cancel it between polls — the scan must stay
    cancellable in the middle of a slow clip.

    Returns (frames, width, height), the size being that of the decoded
    (rotation-corrected) frames.
    """
    from . import ffmpeg

    left = ffmpeg.Deadline(
        timeout, f'gave up after {timeout:.0f}s — the video decoder stopped '
                 'responding').left

    duration = ffmpeg.duration(path, left(), keepalive)
    if not duration:
        # Broken metadata: take what we can from the head of the clip.
        timestamps = [0.5]
    else:
        timestamps = [duration * (i + 1) / (count + 1) for i in range(count)]
    frames, size = [], None
    for t in timestamps:
        try:
            img = ffmpeg.frame_at(path, t, left(), keepalive)
        except (ValueError, RuntimeError):
            continue                    # this frame would not decode
        size = size or img.size
        frames.append(hash_frame(img))
    if not frames:
        raise ValueError("no decodable frames")
    return frames, size[0], size[1]


# ── Sequence distance ──────────────────────────────────────────────────────
def seq_distance(a, b):
    """Compare two frame-hash sequences.

    Returns (symmetric, containment):

      symmetric   — mean of the two directed distances, where a directed
                    distance is the average over the frames of one clip of
                    the closest frame in the other. The reported similarity.
      containment — the smaller of the two directed distances. Small when
                    one clip is a segment of the other (or an image is a
                    frame of a video), even though the symmetric distance
                    is large because most of the longer clip matches
                    nothing.

    Both are floats in [0, 64]. For two single-frame items this reduces to
    the plain Hamming distance.
    """
    if not a or not b:
        return float(HASH_BITS), float(HASH_BITS)

    def directed(src, dst):
        return sum(min(hamming(x, y) for y in dst) for x in src) / len(src)

    a_to_b = directed(a, b)
    b_to_a = directed(b, a)
    return (a_to_b + b_to_a) / 2, min(a_to_b, b_to_a)


def similarity_pct(distance):
    """Distance in bits → a 0-100 similarity figure for display."""
    return round(max(0.0, 1.0 - distance / HASH_BITS) * 100, 1)
