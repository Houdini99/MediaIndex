"""Thumbnail rendering, the video worker's placeholder swap, and the
one-off repair of sideways image thumbnails.

Three defects are pinned here:

* gen_image_thumb ignored the EXIF Orientation tag, so a photo stored
  "sideways with a rotate flag" got a sideways thumbnail, and it turned
  CMYK JPEGs into placeholders because PNG cannot hold CMYK.
* The video worker overwrote its placeholder *in place*, while /thumbs/ is
  served `immutable` for a year — a browser that saw the placeholder kept
  it. The worker now writes a new name and repoints the row.
* app.media.thumb_repair re-renders the thumbnails written before the fix,
  under new names for the same caching reason.
"""
import io
import os
import tempfile

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

from app import IMMUTABLE_CACHE
from app.auth.db import get_user_enc_key, insert_user
from app.config import Config
from app.media import tasks
from app.media.crypto import iter_decrypted
from app.media.db import get_media_by_hash, get_user_id_by_username
from app.media.dupes_db import fetch_hashed_ids, store_hashes
from app.media.ingest import ingest_file
from app.media.processing import gen_image_thumb, versioned_thumb_name
from app.media.thumb_repair import repair_rotated_thumbs

#: gen_placeholder_thumb's fill colour.
PLACEHOLDER = (32, 48, 32)


def _jpeg(size=(400, 200), orientation=None, mode='RGB', color=None):
    """A JPEG whose *stored* pixels are `size`, optionally EXIF-rotated."""
    buf = io.BytesIO()
    img = Image.new(mode, size, color or ((0, 255, 255, 0) if mode == 'CMYK'
                                          else (200, 30, 30)))
    kw = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kw['exif'] = exif
    img.save(buf, format='JPEG', **kw)
    return buf.getvalue()


def _uid(name='thumbs'):
    insert_user(name, generate_password_hash('password123'))
    return get_user_id_by_username(name)


def _ingest(uid, data, ext, private=False):
    fd, tmp = tempfile.mkstemp(dir=Config.UPLOAD_FOLDER, suffix=f'.{ext}')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
    status, digest = ingest_file(tmp, ext, 'thumbtest', uid, private)
    assert status == 'ok'
    return digest


def _thumb_image(thumb, owner=None):
    """Open a stored thumbnail, decrypting it when it is private."""
    path = os.path.join(Config.THUMB_FOLDER, thumb)
    if thumb.endswith('.enc'):
        data = b''.join(iter_decrypted(path, get_user_enc_key(owner)))
        return Image.open(io.BytesIO(data))
    return Image.open(path)


# ── Rendering ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize('orientation', [6, 8])
def test_rotated_photo_gets_an_upright_thumbnail(app, tmp_path, orientation):
    """Stored 400x200 with "rotate 90°": the photo *is* portrait."""
    src = tmp_path / 'phone.jpg'
    src.write_bytes(_jpeg(orientation=orientation))
    gen_image_thumb(str(src), str(tmp_path / 't.png'))
    assert Image.open(tmp_path / 't.png').size == (75, 150)


def test_unrotated_photo_keeps_its_shape(app, tmp_path):
    src = tmp_path / 'plain.jpg'
    src.write_bytes(_jpeg(orientation=1))
    gen_image_thumb(str(src), str(tmp_path / 't.png'))
    assert Image.open(tmp_path / 't.png').size == (150, 75)


def test_cmyk_jpeg_gets_a_real_thumbnail(app, tmp_path):
    src = tmp_path / 'print.jpg'
    src.write_bytes(_jpeg(mode='CMYK'))
    gen_image_thumb(str(src), str(tmp_path / 't.png'))
    thumb = Image.open(tmp_path / 't.png')
    assert thumb.size == (150, 75)
    assert thumb.convert('RGB').getpixel((10, 10)) != PLACEHOLDER


@pytest.mark.parametrize('name, expected', [
    ('abc.png', 'abc.v2.png'),
    ('abc.png.enc', 'abc.v2.png.enc'),
    ('abc.v2.png', 'abc.v3.png'),
    ('abc.v9.png.enc', 'abc.v10.png.enc'),
])
def test_versioned_thumb_name(name, expected):
    assert versioned_thumb_name(name) == expected


# ── Video worker: placeholder → real thumbnail under a new name ────────────
@pytest.fixture()
def fake_render(monkeypatch):
    """Stand in for ffmpeg: 'render' a solid red frame."""
    def render(src, dst, timeout=None):
        Image.new('RGB', (150, 84), (255, 0, 0)).save(dst, format='PNG')
        return dst
    monkeypatch.setattr('app.media.processing.gen_video_thumb', render)


@pytest.fixture()
def captured_jobs(monkeypatch):
    jobs = []
    monkeypatch.setattr('app.media.ingest.enqueue_video_thumb',
                        lambda *a, **kw: jobs.append((a, kw)))
    return jobs


@pytest.mark.parametrize('private', [False, True])
def test_worker_repoints_the_row_instead_of_overwriting(
        app, fake_render, captured_jobs, private):
    uid = _uid()
    digest = _ingest(uid, b'not really a video', 'mp4', private=private)
    placeholder = get_media_by_hash(digest)[2]
    (src, thumb_dst, key), kw = captured_jobs[0]

    new = tasks._process(src, thumb_dst, key)

    row_thumb = get_media_by_hash(digest)[2]
    assert new == row_thumb == versioned_thumb_name(placeholder)
    assert row_thumb != placeholder, "a cached placeholder URL would never refresh"
    assert not os.path.exists(os.path.join(Config.THUMB_FOLDER, placeholder))
    assert _thumb_image(row_thumb, uid).convert('RGB').getpixel((5, 5)) == (255, 0, 0)


def test_worker_announces_the_new_thumbnail(app, fake_render, captured_jobs,
                                            monkeypatch):
    published = []
    monkeypatch.setattr('app.notifications.publish_event', published.append)
    uid = _uid()
    digest = _ingest(uid, b'not really a video 2', 'mp4')
    (src, thumb_dst, key), kw = captured_jobs[0]
    event = kw['notify_event']
    assert event is not None

    tasks._run_job(src, thumb_dst, key, event)

    assert published == [event]
    assert event['thumb'] == get_media_by_hash(digest)[2] \
        == versioned_thumb_name(os.path.basename(thumb_dst))


def test_worker_leaves_a_changed_row_alone(app, fake_render, captured_jobs):
    """The row stopped pointing at the placeholder while the frame rendered
    (a privacy flip renames the thumbnail, for one): the compare-and-set
    misses, the row keeps its new value, and the render is discarded rather
    than left behind as an orphan."""
    from app.media.db import replace_thumb
    uid = _uid()
    digest = _ingest(uid, b'not really a video 3', 'mp4')
    (src, thumb_dst, key), _ = captured_jobs[0]
    assert replace_thumb(thumb_dst, 'moved-meanwhile.png')

    assert tasks._process(src, thumb_dst, key) is None
    assert get_media_by_hash(digest)[2] == 'moved-meanwhile.png'
    stray = os.path.join(Config.THUMB_FOLDER, versioned_thumb_name(thumb_dst))
    assert not os.path.exists(stray)


def test_worker_keeps_the_row_when_the_render_is_a_placeholder(
        app, captured_jobs, monkeypatch):
    """An undecodable video renders as the placeholder again. Swapping one
    placeholder for an identical one under a new name would only churn
    URLs, so the row is left alone and nothing is written."""
    from app.media.processing import gen_placeholder_thumb
    monkeypatch.setattr('app.media.processing.gen_video_thumb',
                        lambda src, dst, timeout=None: gen_placeholder_thumb(dst))
    uid = _uid()
    digest = _ingest(uid, b'undecodable', 'mp4')
    placeholder = get_media_by_hash(digest)[2]
    (src, thumb_dst, key), _ = captured_jobs[0]

    assert tasks._process(src, thumb_dst, key) is None
    assert get_media_by_hash(digest)[2] == placeholder
    assert not os.path.exists(os.path.join(
        Config.THUMB_FOLDER, versioned_thumb_name(placeholder)))


def test_swapped_thumbnail_is_still_cached_immutably(
        client, fake_render, captured_jobs):
    """The fix keeps the caching: a name now maps to one image forever."""
    insert_user('viewer', generate_password_hash('password123'))
    client.post('/login', data={'username': 'viewer', 'password': 'password123'})
    uid = get_user_id_by_username('viewer')
    _ingest(uid, b'not really a video 4', 'mp4')
    (src, thumb_dst, key), _ = captured_jobs[0]
    new = tasks._process(src, thumb_dst, key)

    resp = client.get(f'/thumbs/{new}')
    assert resp.status_code == 200
    assert resp.headers['Cache-Control'] == IMMUTABLE_CACHE
    old = os.path.basename(thumb_dst)
    assert client.get(f'/thumbs/{old}').status_code == 404


# ── One-off repair of sideways image thumbnails ────────────────────────────
@pytest.fixture()
def library(app):
    """One rotated public, one rotated private, one upright public image."""
    uid = _uid('owner')
    rotated = _ingest(uid, _jpeg(orientation=6, color=(1, 2, 3)), 'jpg')
    secret = _ingest(uid, _jpeg(orientation=8, color=(4, 5, 6)), 'jpg',
                     private=True)
    upright = _ingest(uid, _jpeg(orientation=1, color=(7, 8, 9)), 'jpg')
    return uid, rotated, secret, upright


def _thumb_of(digest):
    return get_media_by_hash(digest)[2]


def test_dry_run_reports_and_writes_nothing(library):
    uid, rotated, secret, upright = library
    before = {d: _thumb_of(d) for d in (rotated, secret, upright)}
    listing = sorted(os.listdir(Config.THUMB_FOLDER))

    repaired, skipped = repair_rotated_thumbs(dry_run=True)

    assert {r[1] for r in repaired} == {before[rotated], before[secret]}
    assert skipped == []
    assert {d: _thumb_of(d) for d in before} == before
    assert sorted(os.listdir(Config.THUMB_FOLDER)) == listing


def test_repair_renames_rotated_thumbnails_only(library):
    uid, rotated, secret, upright = library
    old = {d: _thumb_of(d) for d in (rotated, secret, upright)}
    mids = [get_media_by_hash(d)[0] for d in (rotated, secret)]
    for mid in mids:
        store_hashes(mid, [(1, 2, 3)], 'thumb', 1, 10, 10, 100, 'image')

    repaired, skipped = repair_rotated_thumbs()

    assert len(repaired) == 2 and skipped == []
    for d in (rotated, secret):
        new = _thumb_of(d)
        assert new == versioned_thumb_name(old[d])
        assert not os.path.exists(os.path.join(Config.THUMB_FOLDER, old[d]))
        assert _thumb_image(new, uid).size == (75, 150)
    assert _thumb_of(secret).endswith('.enc'), "private thumbnail left plaintext"
    assert _thumb_of(upright) == old[upright]
    # Fingerprints of the re-rendered thumbnails are dropped for rehashing.
    assert not set(mids) & fetch_hashed_ids('thumb', 1)


def test_repair_is_idempotent(library):
    repair_rotated_thumbs()
    repaired, _ = repair_rotated_thumbs(dry_run=True)
    # Orientation is a property of the original, so a second run would
    # re-render again — but only ever the same rotated rows, never others.
    assert len(repaired) == 2


def test_repair_leaves_no_plaintext_in_scratch(library):
    repair_rotated_thumbs()
    assert os.listdir(Config.tmp_dir()) == []
