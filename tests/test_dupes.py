"""Admin near-duplicate finder: hashing, matching, the scan job, and the
two-step (quarantine → purge) resolve flow."""
import io
import os
import time

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

from app.admin import dupes
from app.admin.format import human_size
from app.config import Config
from app.media import dupes_db
from app.media.db import insert_media, get_media_by_id
from app.media.phash import (
    ALGO_VER, hamming, hash_frame, hash_image_file, seq_distance,
    similarity_pct,
)


# ── Fixtures & helpers ─────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_group_cache():
    dupes.invalidate_groups()
    yield
    dupes.invalidate_groups()


@pytest.fixture()
def admin(app):
    """An admin account; returns (id, username, password)."""
    from app.auth.db import insert_user, fetch_user_for_login, set_user_admin
    username, password = 'root', 'password123'
    insert_user(username, generate_password_hash(password))
    uid = fetch_user_for_login(username)[0]
    set_user_admin(uid, True)
    return uid, username, password


@pytest.fixture()
def admin_client(client, admin):
    _uid, username, password = admin
    assert client.post('/login',
                       data={'username': username, 'password': password}
                       ).status_code == 302
    return client


def _gradient(size=(320, 240), shift=0):
    """A structured image — flat colours hash identically and prove nothing."""
    img = Image.new('RGB', size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 7 + shift) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
    return img


def _checkers(size=(320, 240), box=16):
    img = Image.new('RGB', size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (255, 255, 255) if ((x // box) + (y // box)) % 2 else (0, 0, 0)
    return img


def _add_media(uid, name, image, tags='', private=False, thumb_image=None):
    """Write a real media file + thumbnail and index them. Returns the id."""
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(Config.THUMB_FOLDER, exist_ok=True)
    fname, tname = f'{name}.png', f'{name}_t.png'
    image.save(os.path.join(Config.UPLOAD_FOLDER, fname), format='PNG')
    (thumb_image or image).copy().resize((150, 113)).save(
        os.path.join(Config.THUMB_FOLDER, tname), format='PNG')
    assert insert_media(fname, tname, tags, f'hash-{name}', uid, private)
    from app.db_utils import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM media.media WHERE filepath=%s", (fname,))
        return cur.fetchone()[0]


def _run_scan(uid, **overrides):
    """Start a scan and wait for the worker thread to finish."""
    params = dupes.normalize_params({'source': 'thumb', **overrides})
    job_id, message = dupes.start_scan(params, uid)
    assert job_id is not None, message
    deadline = time.time() + 30
    while time.time() < deadline:
        status = dupes.scan_status()
        if not status['running']:
            return status
        time.sleep(0.05)
    raise AssertionError('scan did not finish in time')


# ── Hashing ────────────────────────────────────────────────────────────────
def test_hashes_are_deterministic_and_64_bit():
    img = _gradient()
    first, second = hash_frame(img), hash_frame(img)
    assert first == second
    for value in first:
        assert 0 <= value < 1 << 64


def test_recompressed_copy_stays_close_but_a_different_image_does_not():
    original = _gradient()
    buf = io.BytesIO()
    original.resize((192, 144)).save(buf, format='JPEG', quality=40)
    buf.seek(0)
    recompressed = Image.open(buf).convert('RGB')

    close = hamming(hash_frame(original)[0], hash_frame(recompressed)[0])
    far = hamming(hash_frame(original)[0], hash_frame(_checkers())[0])
    assert close <= dupes.DEFAULT_THRESHOLD
    assert far > dupes.DEFAULT_THRESHOLD


def test_hash_image_file_reports_dimensions(tmp_path):
    path = tmp_path / 'pic.png'
    _gradient((200, 100)).save(path, format='PNG')
    frames, width, height = hash_image_file(str(path))
    assert len(frames) == 1 and (width, height) == (200, 100)


def test_seq_distance_symmetric_and_containment():
    a, b, c = 0b1010, 0b1011, (1 << 63) | 0xFFFF

    # Two single-frame items reduce to plain Hamming distance.
    assert seq_distance([a], [b]) == (1.0, 1.0)

    # A one-frame item that matches one frame of a three-frame item: the
    # symmetric distance is dragged up by the unmatched frames, while the
    # containment distance stays at the good match.
    sym, contain = seq_distance([a], [b, c, c])
    assert contain == 1.0
    assert sym > contain


def test_similarity_pct_endpoints():
    assert similarity_pct(0) == 100.0
    assert similarity_pct(64) == 0.0


# ── Parameters ─────────────────────────────────────────────────────────────
def test_normalize_params_clamps_hostile_input():
    params = dupes.normalize_params({
        'source': 'rm -rf', 'algo': 'sha256', 'threshold': '9999',
        'types': 'image,exe', 'video_frames': '400', 'min_group': '-3',
        'sort': 'drop table',
    })
    assert params['source'] == 'thumb'
    assert params['algo'] == 'phash'
    assert params['threshold'] == dupes.MAX_THRESHOLD
    assert params['types'] == ['image']
    assert params['video_frames'] == 5
    assert params['min_group'] == 2
    assert params['sort'] == 'similarity'


def test_normalize_params_empty_types_means_all():
    assert dupes.normalize_params({})['types'] == list(dupes.TYPES)


# ── Keeper heuristic ───────────────────────────────────────────────────────
def _item(mid, width=100, height=100, bytes_=100, tags=''):
    return {'id': mid, 'width': width, 'height': height, 'bytes': bytes_,
            'tags': tags}


def test_suggest_keeper_prefers_resolution_then_size_then_tags_then_age():
    assert dupes.suggest_keeper([_item(1), _item(2, width=200)])['id'] == 2
    assert dupes.suggest_keeper([_item(1), _item(2, bytes_=999)])['id'] == 2
    assert dupes.suggest_keeper([_item(1), _item(2, tags='a,b')])['id'] == 2
    # All else equal, the oldest (lowest id) wins.
    assert dupes.suggest_keeper([_item(7), _item(3)])['id'] == 3


# ── Scanning ───────────────────────────────────────────────────────────────
def test_scan_fingerprints_public_media(app, admin):
    uid = admin[0]
    _add_media(uid, 'one', _gradient())
    _add_media(uid, 'two', _checkers())

    status = _run_scan(uid)
    assert status['state'] == 'done'
    assert status['hashed'] == 2
    assert status['failed'] == 0
    assert dupes_db.coverage_stats(ALGO_VER)['hashed'] == 2


def test_scan_skips_private_media_entirely(app, admin):
    uid = admin[0]
    _add_media(uid, 'public', _gradient())
    private_id = _add_media(uid, 'secret', _gradient(), private=True)

    status = _run_scan(uid)
    assert status['hashed'] == 1

    _frames, meta = dupes_db.fetch_index(ALGO_VER)
    assert private_id not in meta


def test_second_scan_skips_everything_already_indexed(app, admin):
    uid = admin[0]
    _add_media(uid, 'one', _gradient())
    assert _run_scan(uid)['hashed'] == 1

    again = _run_scan(uid)
    assert again['hashed'] == 0
    assert again['skipped'] == 1


def test_rehash_reindexes_from_scratch(app, admin):
    uid = admin[0]
    _add_media(uid, 'one', _gradient())
    _run_scan(uid)
    assert _run_scan(uid, rehash='1')['hashed'] == 1


def test_missing_file_is_recorded_as_an_error_not_a_crash(app, admin):
    uid = admin[0]
    mid = _add_media(uid, 'gone', _gradient())
    os.remove(os.path.join(Config.THUMB_FOLDER, 'gone_t.png'))

    status = _run_scan(uid)
    assert status['state'] == 'done'
    assert status['failed'] == 1
    assert [row[0] for row in dupes_db.fetch_hash_errors(ALGO_VER)] == [mid]


def test_only_one_scan_runs_at_a_time(app, admin):
    uid = admin[0]
    first = dupes_db.claim_job('{}', 1, uid)
    assert first is not None
    assert dupes_db.claim_job('{}', 1, uid) is None
    dupes_db.finish_job(first, 'done')
    assert dupes_db.claim_job('{}', 1, uid) is not None


def test_cancel_stops_a_running_scan(app, admin):
    uid = admin[0]
    job_id = dupes_db.claim_job('{}', 10, uid)
    assert dupes.cancel_scan() is True
    # The worker learns of the cancel through its progress update.
    assert dupes_db.update_job_progress(job_id, 1, 1, 0, 0) is False


def test_a_broken_item_does_not_end_the_run(app, admin, monkeypatch):
    """One unreadable file must be recorded and stepped over — the failure
    that wedged the first production scan."""
    uid = admin[0]
    _add_media(uid, 'good', _gradient())
    bad = _add_media(uid, 'bad', _checkers())

    real = dupes._fingerprint

    def explode(row, params, keepalive=None):
        if row[0] == bad:
            raise RuntimeError('MoviePy error: failed to read the first frame')
        return real(row, params, keepalive)

    monkeypatch.setattr(dupes, '_fingerprint', explode)
    status = _run_scan(uid)
    assert status['state'] == 'done'
    assert (status['hashed'], status['failed']) == (1, 1)
    assert [row[0] for row in dupes_db.fetch_hash_errors(ALGO_VER)] == [bad]


def test_a_crashing_worker_still_closes_out_its_job(app, admin, monkeypatch):
    """However the worker dies, the job must not be left 'running' — that
    holds the single-runner slot and freezes the UI forever."""
    uid = admin[0]
    _add_media(uid, 'one', _gradient())

    def boom(*_a, **_kw):
        raise RuntimeError('database went away')

    monkeypatch.setattr(dupes_db, 'fetch_hashed_ids', boom)
    status = _run_scan(uid)
    assert status['state'] == 'error'
    assert 'database went away' in (status['error'] or '')
    assert dupes_db.fetch_running_job() is None


def test_cancel_closes_out_a_worker_that_has_stopped_responding(app, admin):
    uid = admin[0]
    job_id = dupes_db.claim_job('{}', 10, uid)
    # Backdate the heartbeat: the worker's process is gone, so nothing will
    # ever read the cancel flag.
    from app.db_utils import db_conn, db_now
    from datetime import timedelta
    with db_conn() as conn:
        conn.cursor().execute(
            "UPDATE media.dupe_job SET heartbeat=%s WHERE id=%s",
            (db_now() - timedelta(minutes=1), job_id))
        conn.commit()

    assert dupes.cancel_scan() is True
    assert dupes_db.fetch_running_job() is None
    assert dupes_db.fetch_last_job()['state'] == 'cancelled'


def test_stale_job_is_reaped(app, admin):
    from datetime import timedelta
    uid = admin[0]
    dupes_db.claim_job('{}', 10, uid)
    assert dupes_db.reap_stale_jobs(timedelta(seconds=-1)) == 1
    assert dupes_db.fetch_running_job() is None


# ── Video decoding ─────────────────────────────────────────────────────────
# Videos are fingerprinted through ffmpeg subprocesses that are killed at a
# deadline or on cancel, so a wedged decoder can never hang the scan thread.
def test_video_decode_returns_the_decoder_result(monkeypatch):
    monkeypatch.setattr(dupes, 'hash_video_frames',
                        lambda path, count, timeout, keepalive:
                        ([(1, 2, 3), (4, 5, 6)], 640, 480))
    frames, width, height = dupes._hash_video('x.mp4', 2, 10)
    assert frames == [(1, 2, 3), (4, 5, 6)]
    assert (width, height) == (640, 480)


def test_video_decode_failure_propagates(monkeypatch):
    def broken(path, count, timeout, keepalive):
        raise ValueError('file is corrupted')

    monkeypatch.setattr(dupes, 'hash_video_frames', broken)
    with pytest.raises(ValueError, match='file is corrupted'):
        dupes._hash_video('x.mp4', 2, 10)


def test_a_hung_decoder_is_killed(app, hanging_ffmpeg):
    started = time.time()
    with pytest.raises(TimeoutError):
        dupes._hash_video('x.mp4', 2, timeout=1)
    assert time.time() - started < 15


def test_cancel_interrupts_a_slow_video(app, hanging_ffmpeg):
    started = time.time()
    with pytest.raises(dupes.ScanCancelled):
        dupes._hash_video('x.mp4', 2, timeout=60, keepalive=lambda: False)
    assert time.time() - started < 15


def test_a_real_video_is_fingerprinted(app, sample_video):
    frames, width, height = dupes._hash_video(sample_video, 3, timeout=60)
    assert len(frames) == 3 and (width, height) == (320, 240)
    assert all(len(f) == 3 for f in frames)


# ── Grouping ───────────────────────────────────────────────────────────────
def _scan_and_group(uid, **params):
    _run_scan(uid)
    return dupes.find_groups(dupes.normalize_params(params))


def test_near_duplicates_are_grouped_and_dissimilar_media_is_not(app, admin):
    uid = admin[0]
    original = _gradient()
    buf = io.BytesIO()
    original.resize((192, 144)).save(buf, format='JPEG', quality=40)
    buf.seek(0)

    a = _add_media(uid, 'a', original)
    b = _add_media(uid, 'b', Image.open(buf).convert('RGB'))
    c = _add_media(uid, 'c', _checkers())

    result = _scan_and_group(uid)
    assert len(result['groups']) == 1
    group = result['groups'][0]
    assert group['ids'] == sorted([a, b])
    assert c not in group['ids']
    assert group['similarity'] >= 90
    assert result['stats']['redundant'] == 1


def test_dismissed_pair_disappears_from_the_grouping(app, admin):
    uid = admin[0]
    original = _gradient()
    a = _add_media(uid, 'a', original)
    b = _add_media(uid, 'b', original.copy())

    assert len(_scan_and_group(uid)['groups']) == 1

    dupes.dismiss_group([a, b], uid)
    assert _scan_and_group(uid)['groups'] == []
    assert dupes.undismiss([(a, b)]) == 1
    assert len(_scan_and_group(uid)['groups']) == 1


def test_min_group_size_filters_pairs(app, admin):
    uid = admin[0]
    original = _gradient()
    _add_media(uid, 'a', original)
    _add_media(uid, 'b', original.copy())
    assert _scan_and_group(uid, min_group='3')['groups'] == []


def test_cross_uploader_filter(app, admin):
    from app.auth.db import insert_user, fetch_user_for_login
    uid = admin[0]
    insert_user('bob', generate_password_hash('password123'))
    other = fetch_user_for_login('bob')[0]

    original = _gradient()
    _add_media(uid, 'a', original)
    _add_media(uid, 'b', original.copy())
    assert _scan_and_group(uid, cross_uploader_only='1')['groups'] == []

    _add_media(other, 'c', original.copy())
    groups = _scan_and_group(uid, cross_uploader_only='1')['groups']
    assert len(groups) == 1 and groups[0]['cross_uploader']


def test_type_filter_excludes_other_types(app, admin):
    uid = admin[0]
    original = _gradient()
    _add_media(uid, 'a', original)
    _add_media(uid, 'b', original.copy())
    assert _scan_and_group(uid, types='video')['groups'] == []


# ── Two-step resolve ───────────────────────────────────────────────────────
def test_resolve_quarantines_losers_and_merges_tags(app, admin):
    uid = admin[0]
    original = _gradient()
    keeper = _add_media(uid, 'a', original, tags='keeper,shared')
    loser = _add_media(uid, 'b', original.copy(), tags='loser,shared')
    _run_scan(uid)

    quarantined, failed = dupes.quarantine_group(keeper, [loser], True, uid)
    assert (quarantined, failed) == (1, 0)

    # The loser is tagged, not deleted.
    loser_row = get_media_by_id(loser)
    assert loser_row is not None
    loser_tags = loser_row['tags'].split(',')
    assert dupes.DUPE_TAG in loser_tags
    assert f'{dupes.DUPE_OF_PREFIX}{keeper}' in loser_tags
    assert os.path.exists(os.path.join(Config.UPLOAD_FOLDER, 'b.png'))

    # The keeper absorbed the loser's tags.
    keeper_tags = get_media_by_id(keeper)['tags'].split(',')
    assert 'loser' in keeper_tags and 'keeper' in keeper_tags
    assert keeper_tags.count('shared') == 1

    assert dupes_db.quarantined_ids() == {loser}


def test_quarantined_items_drop_out_of_the_grid(app, admin):
    uid = admin[0]
    original = _gradient()
    keeper = _add_media(uid, 'a', original)
    loser = _add_media(uid, 'b', original.copy())
    _run_scan(uid)
    assert len(dupes.find_groups(dupes.normalize_params({}))['groups']) == 1

    dupes.quarantine_group(keeper, [loser], False, uid)
    assert dupes.find_groups(dupes.normalize_params({}))['groups'] == []


def test_restore_puts_the_original_tags_back_verbatim(app, admin):
    uid = admin[0]
    original = _gradient()
    keeper = _add_media(uid, 'a', original, tags='keeper')
    loser = _add_media(uid, 'b', original.copy(), tags='one,two')
    _run_scan(uid)
    # insert_media appends the `user:<name>` tag, so compare against what is
    # actually stored rather than what was passed in.
    before = get_media_by_id(loser)['tags']

    dupes.quarantine_group(keeper, [loser], False, uid)
    assert dupes.restore_quarantine([loser], uid) == 1
    assert get_media_by_id(loser)['tags'] == before
    assert dupes_db.quarantined_ids() == set()


def test_purge_deletes_rows_and_files(app, admin):
    uid = admin[0]
    original = _gradient()
    keeper = _add_media(uid, 'a', original)
    loser = _add_media(uid, 'b', original.copy())
    _run_scan(uid)
    dupes.quarantine_group(keeper, [loser], False, uid)

    deleted, failed, freed = dupes.purge_quarantine(None, uid)
    assert (deleted, failed) == (1, 0)
    assert freed > 0
    assert get_media_by_id(loser) is None
    assert get_media_by_id(keeper) is not None
    assert not os.path.exists(os.path.join(Config.UPLOAD_FOLDER, 'b.png'))
    assert not os.path.exists(os.path.join(Config.THUMB_FOLDER, 'b_t.png'))
    assert os.path.exists(os.path.join(Config.UPLOAD_FOLDER, 'a.png'))
    assert dupes_db.quarantined_ids() == set()


def test_resolve_all_dry_run_changes_nothing(app, admin):
    uid = admin[0]
    original = _gradient()
    _add_media(uid, 'a', original)
    _add_media(uid, 'b', original.copy())
    groups = _scan_and_group(uid)['groups']

    preview = dupes.resolve_all(groups, uid, dry_run=True)
    assert preview['quarantined'] == 1 and preview['dry_run'] is True
    assert dupes_db.quarantined_ids() == set()

    applied = dupes.resolve_all(groups, uid, dry_run=False)
    assert applied['quarantined'] == 1
    assert len(dupes_db.quarantined_ids()) == 1


# ── Routes ─────────────────────────────────────────────────────────────────
_JSON_GET = ('/admin/duplicates', '/admin/duplicates/status',
             '/admin/duplicates/groups', '/admin/duplicates/export.csv')
_POSTS = ('/admin/duplicates/scan', '/admin/duplicates/cancel',
          '/admin/duplicates/resolve', '/admin/duplicates/resolve-all',
          '/admin/duplicates/dismiss', '/admin/duplicates/undismiss',
          '/admin/duplicates/quarantine/restore',
          '/admin/duplicates/quarantine/purge')


def test_every_route_is_admin_only(client, logged_in):
    for path in _JSON_GET:
        assert client.get(path).status_code == 403, path
    for path in _POSTS:
        assert client.post(path).status_code == 403, path


def test_routes_redirect_anonymous_visitors(client):
    resp = client.get('/admin/duplicates')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']


def test_page_renders_and_groups_endpoint_answers(admin_client, admin):
    uid = admin[0]
    original = _gradient()
    _add_media(uid, 'a', original)
    _add_media(uid, 'b', original.copy())
    _run_scan(uid)

    page = admin_client.get('/admin/duplicates')
    assert page.status_code == 200
    assert b'Near Duplicates' in page.data

    payload = admin_client.get('/admin/duplicates/groups').get_json()
    assert payload['stats']['groups'] == 1
    group = payload['groups'][0]
    item = group['items'][0]
    assert item['thumb_url'].startswith('/thumbs/')
    assert 'size_h' in item
    # Every human-readable size on this page is formatted server-side, so
    # the card cannot show two different renderings of the same unit next
    # to each other. admin-duplicates.js used to re-implement the formatter
    # in JS and read `group.wasted`; it now reads this.
    assert 'wasted_h' in group
    assert group['wasted_h'] == human_size(group['wasted'])


def test_scan_route_starts_a_job_and_status_reports_it(admin_client, admin):
    _add_media(admin[0], 'a', _gradient())
    resp = admin_client.post('/admin/duplicates/scan',
                             data={'source': 'thumb', 'types': ['image']})
    assert resp.status_code == 302
    deadline = time.time() + 30
    while time.time() < deadline and dupes.scan_status()['running']:
        time.sleep(0.05)
    status = admin_client.get('/admin/duplicates/status').get_json()
    assert status['state'] == 'done' and status['coverage']['hashed'] == 1


def test_resolve_route_validates_its_body(admin_client):
    assert admin_client.post('/admin/duplicates/resolve',
                             json={'ids': []}).status_code == 400
    assert admin_client.post('/admin/duplicates/resolve',
                             json={'ids': [1, 2], 'keep_id': 9}).status_code == 400
    assert admin_client.post('/admin/duplicates/dismiss',
                             json={'ids': [1]}).status_code == 400


def test_export_csv_lists_every_group_member(admin_client, admin):
    uid = admin[0]
    original = _gradient()
    _add_media(uid, 'a', original)
    _add_media(uid, 'b', original.copy())
    _run_scan(uid)

    resp = admin_client.get('/admin/duplicates/export.csv')
    assert resp.status_code == 200
    assert resp.mimetype == 'text/csv'
    body = resp.get_data(as_text=True)
    assert 'keep' in body and 'duplicate' in body
    assert len(body.strip().splitlines()) == 3  # header + two members


def test_purge_route_deletes_quarantined_items(admin_client, admin):
    uid = admin[0]
    original = _gradient()
    keeper = _add_media(uid, 'a', original)
    loser = _add_media(uid, 'b', original.copy())
    _run_scan(uid)
    dupes.quarantine_group(keeper, [loser], False, uid)

    resp = admin_client.post('/admin/duplicates/quarantine/purge',
                             data={'all': '1'}, follow_redirects=True)
    assert resp.status_code == 200
    assert get_media_by_id(loser) is None
