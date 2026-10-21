"""app/admin/integrity.py — the orphan/missing-file cleanup.

Untested until now, and it is the most destructive code in the admin panel:
three one-click actions that delete database rows and files from a 54 GB
media volume, driven entirely by comparing a directory listing against the
media table.

The dangerous failure is not "cleanup missed something" — it is "cleanup
deleted something a row still points at". Most of what follows checks that
referenced files survive, not that orphans die.
"""
import io
import os
import tempfile
import time

from PIL import Image
from werkzeug.security import generate_password_hash

from app.admin import integrity
from app.auth.db import insert_user
from app.config import Config
from app.media.db import get_all_media_paths, get_media_by_hash
from app.media.ingest import ingest_file


def _png(color='navy'):
    buf = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(buf, format='PNG')
    return buf.getvalue()


def _uid(name='integrity-user'):
    insert_user(name, generate_password_hash('password123'))
    from app.media.db import get_user_id_by_username
    return get_user_id_by_username(name)


def _ingest(uid, color='navy', private=False):
    """Index one image; returns (filepath, thumb) as stored on the row."""
    fd, tmp = tempfile.mkstemp(dir=Config.UPLOAD_FOLDER, suffix='.png')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(_png(color))
    status, digest = ingest_file(tmp, 'png', 'integrity', uid, private)
    assert status == 'ok'
    row = get_media_by_hash(digest)
    return row[1], row[2]


def media_abs(name):
    return os.path.join(Config.UPLOAD_FOLDER, name)


def thumb_abs(name):
    return os.path.join(Config.THUMB_FOLDER, name)


def _stray(path, data=b'stray', age=2 * integrity.ORPHAN_GRACE):
    """Write an unreferenced file, aged past the orphan grace period —
    anything younger is presumed to be an upload still in flight."""
    with open(path, 'wb') as fh:
        fh.write(data)
    then = time.time() - age
    os.utime(path, (then, then))


# ── scan() ─────────────────────────────────────────────────────────────────
def test_clean_library_reports_nothing(app):
    _ingest(_uid())
    report = integrity.scan()
    assert report['missing_rows'] == []
    assert report['orphan_files'] == []
    assert report['orphan_thumbs'] == []


def test_detects_a_row_whose_file_vanished(app):
    fp, _th = _ingest(_uid())
    os.remove(media_abs(fp))
    report = integrity.scan()
    assert [r[1] for r in report['missing_rows']] == [fp]


def test_detects_an_unreferenced_file(app):
    _ingest(_uid())
    _stray(media_abs('orphan.png'))
    assert ('orphan.png', 5) in integrity.scan()['orphan_files']


def test_detects_an_unreferenced_thumbnail(app):
    _ingest(_uid())
    _stray(thumb_abs('orphan.png'))
    assert ('orphan.png', 5) in integrity.scan()['orphan_thumbs']


def test_scratch_directory_is_not_an_orphan(app):
    """Config.tmp_dir() is a *directory* inside UPLOAD_FOLDER, and ingest
    now renders private thumbnails through it. The scan lists files only,
    so it must never propose deleting that working set."""
    _ingest(_uid())
    scratch = os.path.join(Config.tmp_dir(), 'ingest-inflight.png')
    with open(scratch, 'wb') as fh:
        fh.write(b'mid-ingest')
    names = [n for n, _ in integrity.scan()['orphan_files']]
    assert '.tmp' not in names
    assert 'ingest-inflight.png' not in names
    assert os.path.exists(scratch)


def test_private_media_is_not_mistaken_for_an_orphan(app):
    """Private rows store '.enc' names; matching is on the stored basename,
    so an encrypted pair must look perfectly healthy."""
    _ingest(_uid(), private=True)
    report = integrity.scan()
    assert report == {'missing_rows': [], 'orphan_files': [], 'orphan_thumbs': [],
                      'placeholder_thumbs': []}


# ── cleanup_missing_rows() ─────────────────────────────────────────────────
def test_cleanup_missing_rows_removes_row_and_its_thumb(app):
    fp, th = _ingest(_uid())
    os.remove(media_abs(fp))
    assert integrity.cleanup_missing_rows() == 1
    assert get_all_media_paths() == []
    assert not os.path.exists(thumb_abs(th)), "orphaned thumbnail left behind"


def test_cleanup_missing_rows_spares_healthy_rows(app):
    uid = _uid()
    gone_fp, _ = _ingest(uid, 'red')
    kept_fp, kept_th = _ingest(uid, 'green')
    os.remove(media_abs(gone_fp))

    assert integrity.cleanup_missing_rows() == 1
    remaining = [fp for _, fp, _ in get_all_media_paths()]
    assert remaining == [kept_fp]
    assert os.path.exists(media_abs(kept_fp))
    assert os.path.exists(thumb_abs(kept_th))


def test_cleanup_missing_rows_is_a_noop_when_healthy(app):
    fp, th = _ingest(_uid())
    assert integrity.cleanup_missing_rows() == 0
    assert os.path.exists(media_abs(fp)) and os.path.exists(thumb_abs(th))


# ── cleanup_orphan_files() / cleanup_orphan_thumbs() ───────────────────────
def test_cleanup_orphan_files_deletes_only_the_orphan(app):
    fp, _ = _ingest(_uid())
    _stray(media_abs('stray.png'))

    assert integrity.cleanup_orphan_files() == 1
    assert not os.path.exists(media_abs('stray.png'))
    assert os.path.exists(media_abs(fp)), "deleted a referenced media file"


def test_cleanup_orphan_thumbs_deletes_only_the_orphan(app):
    _fp, th = _ingest(_uid())
    _stray(thumb_abs('stray.png'))

    assert integrity.cleanup_orphan_thumbs() == 1
    assert not os.path.exists(thumb_abs('stray.png'))
    assert os.path.exists(thumb_abs(th)), "deleted a referenced thumbnail"


def test_cleanup_orphan_files_never_touches_the_scratch_dir(app):
    """The nightmare case: a cleanup running while an upload is mid-encrypt
    must not delete the in-flight plaintext out from under it."""
    _ingest(_uid())
    scratch = os.path.join(Config.tmp_dir(), 'ingest-inflight.png')
    with open(scratch, 'wb') as fh:
        fh.write(b'mid-ingest')
    integrity.cleanup_orphan_files()
    assert os.path.exists(scratch)
    assert os.path.isdir(Config.tmp_dir())


def test_cleanup_is_idempotent(app):
    _ingest(_uid())
    _stray(media_abs('stray.png'))
    assert integrity.cleanup_orphan_files() == 1
    assert integrity.cleanup_orphan_files() == 0


def test_a_fresh_unreferenced_file_is_not_an_orphan_yet(app):
    """An upload is written into UPLOAD_FOLDER under a temp name before its
    row exists. Reporting it as an orphan let "delete orphaned files" remove
    it out from under the request that was still writing it."""
    _stray(media_abs('tmpupload.mp4'), age=0)
    _stray(thumb_abs('fresh.png'), age=0)
    report = integrity.scan()
    assert 'tmpupload.mp4' not in [n for n, _ in report['orphan_files']]
    assert 'fresh.png' not in [n for n, _ in report['orphan_thumbs']]
    assert integrity.cleanup_orphan_files() == 0
    assert integrity.cleanup_orphan_thumbs() == 0
    assert os.path.exists(media_abs('tmpupload.mp4'))
    assert os.path.exists(thumb_abs('fresh.png'))


# ── folder_stats() ─────────────────────────────────────────────────────────
def test_folder_stats_counts_files_not_directories(app):
    _ingest(_uid())
    Config.tmp_dir()                       # ensure the directory exists
    count, total = integrity.folder_stats(Config.UPLOAD_FOLDER)
    assert count == 1 and total > 0


def test_folder_stats_of_a_missing_folder_is_zero(app, tmp_path):
    assert integrity.folder_stats(str(tmp_path / 'nope')) == (0, 0)


# ── Route wiring ───────────────────────────────────────────────────────────
def test_cleanup_route_rejects_an_unknown_target(app, client):
    """Guards the dispatch: an unrecognised target must do nothing at all
    rather than fall through to a destructive branch."""
    insert_user('admin-int', generate_password_hash('password123'))
    from app.auth.db import fetch_all_users, set_user_admin
    uid = [u[0] for u in fetch_all_users() if u[1] == 'admin-int'][0]
    set_user_admin(uid, True)
    client.post('/login', data={'username': 'admin-int', 'password': 'password123'})

    fp, _ = _ingest(uid)
    resp = client.post('/admin/integrity/cleanup', data={'target': 'nonsense'},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b'Unknown cleanup target' in resp.data
    assert os.path.exists(media_abs(fp))


# ── Placeholder thumbnails ─────────────────────────────────────────────────
def _make_placeholder(thumb, owner=None):
    """Overwrite a stored thumbnail with the placeholder, as a lost or
    failed render leaves it."""
    from app.auth.db import get_user_enc_key
    from app.media.crypto import encrypt_file
    from app.media.processing import gen_placeholder_thumb
    if thumb.endswith('.enc'):
        tmp = os.path.join(Config.tmp_dir(), 'ph.png')
        gen_placeholder_thumb(tmp)
        encrypt_file(tmp, thumb_abs(thumb), get_user_enc_key(owner))
        os.remove(tmp)
    else:
        gen_placeholder_thumb(thumb_abs(thumb))


def test_real_thumbnails_are_not_placeholders(app):
    _ingest(_uid(), 'navy')
    _ingest(_uid('other'), 'teal', private=True)
    assert integrity.scan()['placeholder_thumbs'] == []


def test_placeholders_are_reported_public_and_private(app):
    uid = _uid()
    _, public_th = _ingest(uid, 'navy')
    _, private_th = _ingest(uid, 'teal', private=True)
    _make_placeholder(public_th)
    _make_placeholder(private_th, uid)
    reported = {th for _, _, th in integrity.scan()['placeholder_thumbs']}
    assert reported == {public_th, private_th}


def test_rerender_fixes_images_and_queues_videos(app, monkeypatch):
    from app.media.processing import is_placeholder_thumb
    jobs = []
    monkeypatch.setattr('app.media.ingest.enqueue_video_thumb',
                        lambda *a, **kw: None)          # upload: keep placeholder
    monkeypatch.setattr('app.media.tasks.enqueue_video_thumb',
                        lambda *a, **kw: jobs.append(a))
    uid = _uid()
    _, image_th = _ingest(uid, 'navy')
    _make_placeholder(image_th)
    fd, tmp = tempfile.mkstemp(dir=Config.UPLOAD_FOLDER, suffix='.mp4')
    os.close(fd)
    status, digest = ingest_file(tmp, 'mp4', 'clip', uid, False)
    video_th = get_media_by_hash(digest)[2]
    assert is_placeholder_thumb(thumb_abs(video_th))

    queued, rerendered, failed = integrity.rerender_placeholder_thumbs()

    assert (queued, rerendered, failed) == (1, 1, 0)
    assert jobs and jobs[0][1] == thumb_abs(video_th)
    remaining = [th for _, _, th in integrity.scan()['placeholder_thumbs']]
    assert remaining == [video_th], "the image should have a real thumbnail now"


def test_integrity_page_offers_the_rerender(client):
    insert_user('root', generate_password_hash('password123'))   # first = admin
    client.post('/login', data={'username': 'root', 'password': 'password123'})
    from app.media.db import get_user_id_by_username
    _, th = _ingest(get_user_id_by_username('root'), 'navy')
    _make_placeholder(th)

    page = client.get('/admin/integrity').get_data(as_text=True)
    assert 'Placeholder thumbnails' in page and 'Re-render' in page

    resp = client.post('/admin/integrity/cleanup',
                       data={'target': 'placeholder_thumbs'}, follow_redirects=True)
    assert 'Re-rendered 1 image thumbnail' in resp.get_data(as_text=True)
    assert integrity.scan()['placeholder_thumbs'] == []
