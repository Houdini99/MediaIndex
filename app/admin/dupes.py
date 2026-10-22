"""Near-duplicate finder — the engine behind the admin Duplicates tab.

Two halves:

  1. **The scan.** Fingerprints every public media row (app.media.phash) and
     stores the frame hashes. Slow the first time — every video is decoded —
     so it runs on a daemon thread while its progress lives in the
     media.dupe_job table, where both gunicorn workers can read it.

  2. **The grouping.** Loads the fingerprint index, finds every pair of items
     within the requested distance, and unions the surviving pairs into
     groups. Cheap enough (sub-second at this library's size) to run per
     request behind a short-lived cache.

Private media is out of scope by construction: nothing here ever selects a
private row, so no encrypted file is read and no private thumbnail is
exposed.

Resolving is deliberately two-step. "Resolve" only *quarantines* the losers —
it tags them and records them in media.dupe_quarantine, keeping a copy of
their original tags — and a separate, explicit purge deletes them. Every step
before the purge is reversible.
"""
import json
import logging
import os
import threading
import time
from datetime import timedelta

import numpy as np

from ..config import Config
from ..media import dupes_db
from ..media.ffmpeg import Cancelled
from ..media.db import (
    delete_media,
    get_media_by_id,
    media_path,
    thumb_path,
    update_media_tags,
)
from ..media.phash import (
    ALGO_VER,
    DEFAULT_VIDEO_FRAMES,
    HASH_BITS,
    hash_image_file,
    hash_video_frames,
    seq_distance,
    similarity_pct,
)
from ..media.tagging import tag_list, tag_str
from ..request_parsing import form_flag

# Tags applied to a quarantined item so it is also findable in the gallery.
DUPE_TAG = 'dupe'
DUPE_OF_PREFIX = 'dupe-of:'

# A running job whose worker stopped heartbeating for this long is assumed
# dead (its process was killed) and its slot is released.
STALE_JOB_AGE = timedelta(minutes=5)

# Frame-pair search safety valves. A library of visually flat videos (black
# intros, solid-colour screens) can produce a combinatorial explosion of
# candidate pairs; past this many the search stops and the UI says so.
MAX_CANDIDATE_PAIRS = 250_000
# Bytes per pairwise block — sizes the chunking of the distance matrix.
_BLOCK_BYTES = 32 * 1024 * 1024

# How long a job may go without a heartbeat before an explicit cancel gives
# up on the worker and closes the job out itself.
UNRESPONSIVE_AFTER = timedelta(seconds=30)

_log = logging.getLogger(__name__)

SOURCES = ('thumb', 'file')
ALGOS = ('phash', 'dhash', 'ahash', 'strict')
TYPES = ('image', 'gif', 'video')
VIDEO_FRAME_CHOICES = (3, 5, 9)
MAX_THRESHOLD = 16
DEFAULT_THRESHOLD = 8

_GROUP_CACHE_TTL = 120
_group_cache = {}
_group_cache_lock = threading.Lock()


# ── Parameters ─────────────────────────────────────────────────────────────
def normalize_params(raw):
    """Coerce untrusted form/query input into a valid parameter set.

    Every value is clamped to a known-good choice rather than rejected, so a
    hand-edited request can never put the scanner or the matcher into an
    undefined state.
    """
    def _one_of(value, choices, default):
        value = (value or '').strip().lower()
        return value if value in choices else default

    types = [t for t in (raw.get('types') or '').split(',') if t in TYPES]
    try:
        threshold = int(raw.get('threshold', DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    try:
        frames = int(raw.get('video_frames', DEFAULT_VIDEO_FRAMES))
    except (TypeError, ValueError):
        frames = DEFAULT_VIDEO_FRAMES
    try:
        min_group = int(raw.get('min_group', 2))
    except (TypeError, ValueError):
        min_group = 2
    return {
        'source': _one_of(raw.get('source'), SOURCES, 'thumb'),
        'algo': _one_of(raw.get('algo'), ALGOS, 'phash'),
        'threshold': max(0, min(MAX_THRESHOLD, threshold)),
        'types': types or list(TYPES),
        'video_frames': frames if frames in VIDEO_FRAME_CHOICES else DEFAULT_VIDEO_FRAMES,
        'min_group': max(2, min(50, min_group)),
        'rehash': form_flag(raw.get('rehash')),
        'cross_uploader_only': form_flag(raw.get('cross_uploader_only')),
        'sort': _one_of(raw.get('sort'),
                        ('similarity', 'wasted', 'size', 'newest'), 'similarity'),
    }


# ── The scan ───────────────────────────────────────────────────────────────
class ScanCancelled(Exception):
    """Raised inside a long item when the admin cancels the scan."""


def _hash_video(path, count, timeout, keepalive=None):
    """Fingerprint a video with a deadline and a cancel hook.

    The decode itself runs as ffmpeg subprocesses that are killed at the
    deadline or on cancel (app.media.ffmpeg) — the thumbnail worker and the
    staging scanner decode the same untrusted video with the same hazard.
    This wrapper keeps the scan's own contract: a cancel surfaces as
    `ScanCancelled` rather than the generic `Cancelled`.
    """
    try:
        return hash_video_frames(path, count, timeout=timeout,
                                 keepalive=keepalive)
    except Cancelled:
        raise ScanCancelled() from None


def _fingerprint(row, params, keepalive=None):
    """Fingerprint one media row → (frames, width, height, ftype).

    Videos are sampled frame by frame from the original file whichever
    source is configured: their thumbnail is a single poster frame, which is
    precisely what multi-frame matching exists to improve on. Stills follow
    the configured source — the 150px thumbnail (fast, always present) or
    the full-resolution original (slower, more precise on subtle crops).
    """
    filepath, thumb, ftype = row.filepath, row.thumb, row.ftype

    if ftype == 'video':
        path = media_path(filepath)
        if not os.path.exists(path):
            raise FileNotFoundError(f"media file missing: {os.path.basename(path)}")
        frames, width, height = _hash_video(
            path, params['video_frames'], Config.DUPE_ITEM_TIMEOUT, keepalive)
        return frames, width, height, ftype

    if params['source'] == 'file':
        path = media_path(filepath)
    else:
        if not thumb:
            raise FileNotFoundError("no thumbnail on this row")
        path = thumb_path(thumb)
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing on disk: {os.path.basename(path)}")
    frames, width, height = hash_image_file(path)
    if params['source'] != 'file':
        # The hashes come from the 150px thumbnail, but the dimensions must
        # describe the *original*: they drive the "most pixels" keeper rule
        # and are shown as the item's resolution. Reading them is a header
        # parse, not a decode.
        width, height = _original_size(filepath) or (width, height)
    return frames, width, height, ftype


def _original_size(filepath):
    """(width, height) of the stored media file, or None if unreadable."""
    from PIL import Image
    try:
        with Image.open(media_path(filepath)) as img:
            return img.size
    except Exception:
        return None


def _file_bytes(filepath):
    path = media_path(filepath)
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def start_scan(params, user_id):
    """Claim the single scan slot and start the worker thread.

    Returns (job_id, message). job_id is None when a scan is already
    running.
    """
    dupes_db.reap_stale_jobs(STALE_JOB_AGE)
    if params['rehash']:
        dupes_db.clear_hash_index()

    candidates = [
        row for row in dupes_db.fetch_public_media()
        if row.ftype in params['types']
    ]
    job_id = dupes_db.claim_job(json.dumps(params), len(candidates), user_id)
    if job_id is None:
        return None, 'A scan is already running.'

    threading.Thread(
        target=_scan_worker,
        args=(job_id, params, candidates),
        name='dupe-scan',
        daemon=True,
    ).start()
    return job_id, f'Scan started over {len(candidates)} item(s).'


def _scan_worker(job_id, params, candidates):
    """Fingerprint every candidate, then close the job out.

    The job row MUST reach a terminal state on every exit path. When it
    doesn't, the single-runner slot stays occupied by a job whose worker no
    longer exists, the progress bar freezes, and Cancel has nothing left to
    talk to — which is exactly what an earlier logging bug caused here. So
    finalisation lives in `finally`, runs before anything that could raise,
    and is itself guarded.
    """
    processed = hashed = skipped = failed = 0
    state, error = 'done', None
    last_beat = [0.0]

    def keepalive(force=False):
        """Heartbeat at most every 2 s; False means "stop, you're cancelled"."""
        now = time.monotonic()
        if not force and now - last_beat[0] < 2.0:
            return True
        last_beat[0] = now
        return dupes_db.update_job_progress(
            job_id, processed, hashed, skipped, failed)

    try:
        done = set() if params['rehash'] else dupes_db.fetch_hashed_ids(
            params['source'], ALGO_VER)
        for row in candidates:
            mid, filepath = row[0], row[1]
            processed += 1
            if mid in done:
                skipped += 1
            else:
                try:
                    frames, width, height, ftype = _fingerprint(
                        row, params, keepalive)
                    dupes_db.store_hashes(
                        mid, frames, params['source'], ALGO_VER, width, height,
                        _file_bytes(filepath), ftype)
                    hashed += 1
                except ScanCancelled:
                    state = 'cancelled'
                    break
                except Exception as exc:
                    # One unreadable file must never end the run: record it
                    # against the row so incremental scans skip it, and move on.
                    _log.warning("dupes: fingerprint failed for media %s: %s",
                                 mid, exc)
                    dupes_db.store_hash_error(
                        mid, params['source'], ALGO_VER, row.ftype,
                        f"{type(exc).__name__}: {exc}")
                    failed += 1

            if not keepalive(force=processed == len(candidates)):
                state = 'cancelled'
                break
    except Exception as exc:
        state, error = 'error', exc
    finally:
        try:
            dupes_db.update_job_progress(job_id, processed, hashed, skipped, failed)
        except Exception:
            pass
        try:
            dupes_db.finish_job(job_id, state, error)
        except Exception:
            _log.exception("dupes: could not finalise scan job %s", job_id)
        if error is not None:
            _log.error("dupes: scan job %s failed: %s", job_id, error,
                       exc_info=error)
        invalidate_groups()


def cancel_scan():
    """Ask the worker to stop — and stop it ourselves if it can't answer.

    Normally the worker notices `cancel_requested` at its next heartbeat and
    exits cleanly. If it has already died, nothing would ever read the flag
    and the job would sit 'running' until the stale-job reaper caught it
    minutes later, with Cancel appearing to do nothing. So a cancel against
    a worker that has gone quiet closes the job out directly.
    """
    if not dupes_db.request_cancel():
        return False
    dupes_db.finish_unresponsive_job(
        UNRESPONSIVE_AFTER, 'cancelled',
        'cancelled after the scan worker stopped responding')
    return True


def scan_status():
    """Progress payload for the poller."""
    dupes_db.reap_stale_jobs(STALE_JOB_AGE)
    job = dupes_db.fetch_running_job() or dupes_db.fetch_last_job()
    coverage = dupes_db.coverage_stats(ALGO_VER)
    payload = {
        'running': False,
        'coverage': {
            **coverage,
            'last_hashed_at': coverage['last_hashed_at'].isoformat()
            if coverage['last_hashed_at'] else None,
        },
    }
    if not job:
        payload['state'] = 'idle'
        return payload

    total = job['total'] or 0
    processed = job['processed'] or 0
    elapsed = None
    if job['started_at']:
        end = job['finished_at'] or job['heartbeat'] or job['started_at']
        elapsed = max(0.0, (end - job['started_at']).total_seconds())
    eta = None
    if job['state'] == 'running' and elapsed and processed:
        eta = max(0, round(elapsed / processed * (total - processed)))
    payload.update({
        'running': job['state'] == 'running',
        'state': job['state'],
        'job_id': job['id'],
        'total': total,
        'processed': processed,
        'hashed': job['hashed'],
        'skipped': job['skipped'],
        'failed': job['failed'],
        'percent': round(processed / total * 100, 1) if total else 0.0,
        'cancel_requested': bool(job['cancel_requested']),
        'error': job['error'],
        'elapsed': round(elapsed) if elapsed is not None else None,
        'eta': eta,
        'params': json.loads(job['params']) if job['params'] else None,
        'started_at': job['started_at'].isoformat() if job['started_at'] else None,
        'finished_at': job['finished_at'].isoformat() if job['finished_at'] else None,
    })
    return payload


# ── Matching ───────────────────────────────────────────────────────────────
def _hash_columns(frames, algo):
    """The hash column(s) the requested algorithm compares on."""
    if algo == 'dhash':
        return [np.array([f[2] for f in frames], dtype=np.uint64)]
    if algo == 'ahash':
        return [np.array([f[3] for f in frames], dtype=np.uint64)]
    if algo == 'strict':
        # Both pHash and dHash must agree — the pair distance is the worse
        # of the two, so a coincidental match on one hash is not enough.
        return [np.array([f[1] for f in frames], dtype=np.uint64),
                np.array([f[2] for f in frames], dtype=np.uint64)]
    return [np.array([f[1] for f in frames], dtype=np.uint64)]


def _candidate_pairs(frames, algo, threshold):
    """Media-id pairs with at least one frame pair within `threshold`.

    Exact, not approximate: the frame array is scanned pairwise in blocks
    sized to a fixed memory budget. At this library's size (~10k frames,
    50M comparisons) numpy does it in well under a second, which is why no
    LSH banding is needed.

    Returns (pairs, truncated).
    """
    n = len(frames)
    if n < 2:
        return set(), False
    owners = np.array([f[0] for f in frames], dtype=np.int64)
    columns = _hash_columns(frames, algo)

    block = max(1, min(n, _BLOCK_BYTES // (n * 8)))
    pairs = set()
    truncated = False
    for start in range(0, n, block):
        stop = min(n, start + block)
        dist = None
        for col in columns:
            d = np.bitwise_count(col[start:stop, None] ^ col[None, :])
            dist = d if dist is None else np.maximum(dist, d)
        # Upper triangle only, and never a frame against another frame of
        # the same item.
        rows = np.arange(start, stop)[:, None]
        cols = np.arange(n)[None, :]
        hit = (dist <= threshold) & (cols > rows) & \
              (owners[start:stop, None] != owners[None, :])
        rows_i, cols_j = np.nonzero(hit)
        # Bound the work per block too: a wall of visually identical frames
        # can hit millions of times, and only the pair budget matters.
        budget = MAX_CANDIDATE_PAIRS - len(pairs) + 1
        if len(rows_i) > budget:
            rows_i, cols_j = rows_i[:budget], cols_j[:budget]
            truncated = True
        left, right = owners[start + rows_i], owners[cols_j]
        pairs.update(zip(np.minimum(left, right).tolist(),
                         np.maximum(left, right).tolist()))
        if truncated or len(pairs) > MAX_CANDIDATE_PAIRS:
            truncated = True
            break
    return pairs, truncated


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:      # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def suggest_keeper(items):
    """The item a group should keep: most pixels, then largest file, then
    most tags, then oldest (lowest id)."""
    return min(items, key=lambda it: (
        -((it['width'] or 0) * (it['height'] or 0)),
        -(it['bytes'] or 0),
        -len(tag_list(it['tags'])),
        it['id'],
    ))


def _index_fingerprint(coverage, dismissed, quarantined):
    last = coverage['last_hashed_at']
    return (coverage['hashed'], coverage['failed'],
            last.isoformat() if last else None,
            len(dismissed), len(quarantined))


def invalidate_groups():
    with _group_cache_lock:
        _group_cache.clear()


def find_groups(params):
    """Group near-duplicates under `params`.

    Returns a dict with `groups`, `stats` and `truncated`. Cached briefly and
    keyed on the parameters plus a fingerprint of the index, so a finished
    scan, a dismissal or a quarantine change is picked up immediately.
    """
    coverage = dupes_db.coverage_stats(ALGO_VER)
    dismissed = dupes_db.fetch_dismissed_pairs()
    quarantined = dupes_db.quarantined_ids()
    key = (params['algo'], params['threshold'], tuple(sorted(params['types'])),
           params['min_group'], params['cross_uploader_only'], params['sort'],
           _index_fingerprint(coverage, dismissed, quarantined))

    now = time.monotonic()
    with _group_cache_lock:
        hit = _group_cache.get(key)
        if hit and now - hit[0] < _GROUP_CACHE_TTL:
            return hit[1]

    result = _compute_groups(params, dismissed, quarantined)
    with _group_cache_lock:
        if len(_group_cache) > 8:
            _group_cache.clear()
        _group_cache[key] = (now, result)
    return result


def _compute_groups(params, dismissed, quarantined):
    from ..auth.db import fetch_all_users

    frames, meta = dupes_db.fetch_index(ALGO_VER)
    media_rows = {r[0]: r for r in dupes_db.fetch_public_media()}
    usernames = {uid: name for uid, name, _admin in fetch_all_users()}

    # Quarantined items are already resolved — keep them out of the grid so
    # a group does not reappear the moment it is dealt with.
    keep = {mid for mid in meta
            if mid not in quarantined
            and mid in media_rows
            and (meta[mid][5] or 'image') in params['types']}
    frames = [f for f in frames if f[0] in keep]

    pairs, truncated = _candidate_pairs(frames, params['algo'], params['threshold'])

    by_media = {}
    for mid, ph, dh, ah in frames:
        by_media.setdefault(mid, []).append((ph, dh, ah))

    # Which hash the sequence distance is measured on (index into the frame
    # tuple); 'strict' reports on pHash, having already required both.
    hash_idx = {'dhash': 1, 'ahash': 2}.get(params['algo'], 0)

    scored = {}
    for a, b in pairs:
        if (a, b) in dismissed:
            continue
        seq_a = [f[hash_idx] for f in by_media.get(a, ())]
        seq_b = [f[hash_idx] for f in by_media.get(b, ())]
        d_sym, d_contain = seq_distance(seq_a, seq_b)
        if d_sym <= params['threshold'] or d_contain <= params['threshold']:
            scored[(a, b)] = (d_sym, d_contain)

    uf = _UnionFind()
    for a, b in scored:
        uf.union(a, b)

    clusters = {}
    for mid in {m for pair in scored for m in pair}:
        clusters.setdefault(uf.find(mid), []).append(mid)

    groups = []
    for root, ids in clusters.items():
        if len(ids) < params['min_group']:
            continue
        items = [_item(mid, meta, media_rows, usernames) for mid in sorted(ids)]
        uploaders = {it['uploader'] for it in items}
        if params['cross_uploader_only'] and len(uploaders) < 2:
            continue
        keeper = suggest_keeper(items)
        for it in items:
            it['suggested'] = it['id'] == keeper['id']
        group_pairs = [
            {'a': a, 'b': b,
             'distance': round(d_sym, 2),
             'similarity': similarity_pct(d_sym),
             'subset': d_contain <= params['threshold'] and d_sym > params['threshold']}
            for (a, b), (d_sym, d_contain) in scored.items()
            if a in ids and b in ids
        ]
        worst = max((p['distance'] for p in group_pairs), default=float(HASH_BITS))
        best = min((p['distance'] for p in group_pairs), default=float(HASH_BITS))
        wasted = sum(it['bytes'] or 0 for it in items if it['id'] != keeper['id'])
        groups.append({
            'key': f"g{min(ids)}",
            'ids': sorted(ids),
            'items': items,
            'pairs': sorted(group_pairs, key=lambda p: p['distance']),
            'keeper_id': keeper['id'],
            'similarity': similarity_pct(worst),
            'best_similarity': similarity_pct(best),
            'distance': round(worst, 2),
            'wasted': wasted,
            'size': len(items),
            'types': sorted({it['ftype'] for it in items}),
            'uploaders': sorted(uploaders),
            'cross_uploader': len(uploaders) > 1,
            'has_subset': any(p['subset'] for p in group_pairs),
            'newest': max((it['upload_date'] or '' for it in items), default=''),
        })

    if params['sort'] == 'newest':
        groups.sort(key=lambda g: g['newest'], reverse=True)
    else:
        groups.sort(key={
            'similarity': lambda g: (-g['similarity'], -g['wasted']),
            'wasted': lambda g: (-g['wasted'], -g['similarity']),
            'size': lambda g: (-g['size'], -g['similarity']),
        }[params['sort']])

    return {
        'groups': groups,
        'truncated': truncated,
        'stats': {
            'groups': len(groups),
            'items': sum(g['size'] for g in groups),
            'redundant': sum(g['size'] - 1 for g in groups),
            'wasted': sum(g['wasted'] for g in groups),
            'compared': len(keep),
            'cross_uploader': sum(1 for g in groups if g['cross_uploader']),
        },
    }


def _item(mid, meta, media_rows, usernames):
    source, frames, width, height, bytes_, ftype, hashed_at = meta[mid]
    _id, filepath, thumb, uploaded_by, upload_date, tags, _ftype = media_rows[mid]
    return {
        'id': mid,
        'filepath': os.path.basename(filepath or ''),
        'thumb': os.path.basename(thumb or ''),
        'tags': tags or '',
        'uploader': usernames.get(uploaded_by, 'Unknown'),
        'uploader_id': uploaded_by,
        'upload_date': upload_date.isoformat() if upload_date else None,
        'width': width,
        'height': height,
        'bytes': bytes_,
        'ftype': ftype or 'image',
        'frames': frames,
        'source': source,
        'suggested': False,
    }


# ── Resolving: quarantine, restore, purge ──────────────────────────────────
def _is_marker(tag):
    return tag == DUPE_TAG or tag.startswith(DUPE_OF_PREFIX)


def quarantine_group(keep_id, loser_ids, merge_tags, user_id, distance=None):
    """Step one of the resolve: mark the losers, never delete them.

    Each loser keeps its files and its row; it gains the `dupe` and
    `dupe-of:<keeper>` tags (so it is also findable in the gallery) and a
    quarantine record holding its exact previous tag string. With
    `merge_tags` the losers' tags are unioned onto the keeper first, so
    purging later cannot lose tagging work.

    Returns (quarantined, failed).
    """
    keeper = get_media_by_id(keep_id)
    if not keeper:
        return 0, len(loser_ids)

    merged = tag_list(keeper['tags'])
    quarantined = failed = 0
    for mid in loser_ids:
        if mid == keep_id:
            continue
        row = get_media_by_id(mid)
        if not row:
            failed += 1
            continue
        original = row['tags'] or ''
        if merge_tags:
            for tag in tag_list(original):
                if not _is_marker(tag) and tag not in merged:
                    merged.append(tag)
        marked = [t for t in tag_list(original) if not _is_marker(t)]
        marked += [DUPE_TAG, f'{DUPE_OF_PREFIX}{keep_id}']
        if not update_media_tags(mid, tag_str(marked), user_id, is_admin=True):
            failed += 1
            continue
        dupes_db.add_quarantine(mid, keep_id, distance, user_id, original)
        quarantined += 1

    if merge_tags and quarantined:
        update_media_tags(keep_id, tag_str(merged), user_id, is_admin=True)
    if quarantined:
        invalidate_groups()
    return quarantined, failed


def restore_quarantine(ids, user_id):
    """Undo a quarantine: the original tag string is written back verbatim."""
    rows = dupes_db.fetch_quarantine_rows(ids)
    restored = 0
    for mid, tags_backup, _keeper in rows:
        if get_media_by_id(mid) and update_media_tags(
                mid, tags_backup or '', user_id, is_admin=True):
            restored += 1
    dupes_db.remove_quarantine([r[0] for r in rows])
    if rows:
        invalidate_groups()
    return restored


def purge_quarantine(ids, user_id):
    """Step two: permanently delete quarantined items, files and all.

    Deletion goes through media.db.delete_media, which removes the media
    file and the thumbnail as well as the row — so a purge leaves no orphans
    behind for the integrity check to find.

    Returns (deleted, failed, freed_bytes).
    """
    rows = dupes_db.fetch_quarantine_rows(ids)
    deleted = failed = freed = 0
    for mid, _tags, _keeper in rows:
        row = get_media_by_id(mid)
        size = _file_bytes(row['filepath']) if row else 0
        if delete_media(mid, user_id, is_admin=True):
            deleted += 1
            freed += size or 0
        else:
            failed += 1
    # Rows for deleted media cascade away; clean up any that survived a
    # failed delete only if their media row is really gone.
    leftovers = [mid for mid, _t, _k in rows if not get_media_by_id(mid)]
    dupes_db.remove_quarantine(leftovers)
    if deleted:
        invalidate_groups()
    return deleted, failed, freed


def dismiss_group(ids, user_id):
    """Mark every pair inside a group as "not duplicates"."""
    ids = sorted(set(int(i) for i in ids))
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
    added = dupes_db.dismiss_pairs(pairs, user_id)
    if added:
        invalidate_groups()
    return added


def undismiss(pairs):
    removed = dupes_db.undismiss_pairs(pairs)
    if removed:
        invalidate_groups()
    return removed


def resolve_all(groups, user_id, merge_tags=False, dry_run=True):
    """Apply the suggested keeper to every group in `groups`.

    The UI always previews first: `dry_run` reports what would happen
    without touching a single row.
    """
    would_quarantine = sum(g['size'] - 1 for g in groups)
    would_free = sum(g['wasted'] for g in groups)
    if dry_run:
        return {'groups': len(groups), 'quarantined': would_quarantine,
                'failed': 0, 'bytes': would_free, 'dry_run': True}
    quarantined = failed = 0
    for group in groups:
        losers = [i for i in group['ids'] if i != group['keeper_id']]
        q, f = quarantine_group(group['keeper_id'], losers, merge_tags,
                                user_id, group['distance'])
        quarantined += q
        failed += f
    return {'groups': len(groups), 'quarantined': quarantined, 'failed': failed,
            'bytes': would_free, 'dry_run': False}


# ── Misc helpers for the routes ────────────────────────────────────────────
# Kept because it binds ALGO_VER: the route asking for the error list has no
# business knowing which fingerprint version is current. The three neighbours
# that once sat here — quarantine_list/quarantine_totals/dismissed_list —
# were argument-free renames of a dupes_db call and added nothing, so the
# routes now call dupes_db directly.
def hash_errors():
    return dupes_db.fetch_hash_errors(ALGO_VER)


def library_scale():
    """Rough item counts per type, so the scan form can warn about how long
    a video pass will take before it is started."""
    stored = dupes_db.count_public_media_by_type()
    return {t: stored.get(t, 0) for t in TYPES}
