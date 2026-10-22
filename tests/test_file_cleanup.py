"""What is left on disk when a step that writes files fails halfway.

Each failure path below removes the files its step had already written,
through app.files.remove_if_exists; none of them ran anywhere else in the
suite. The admin cases are the two user-deletion branches that drop files:
private media whose owner has no key, and staged thumbnails.
"""
import os
import tempfile

import pytest
from werkzeug.security import generate_password_hash

from app.auth.db import fetch_user_by_id, fetch_user_for_login, insert_user, set_user_admin
from app.config import Config
from app.db_utils import db_conn
from app.media import ingest, staging_db, tasks, thumb_repair
from app.media.db import get_media_by_hash, get_media_by_id, media_path, thumb_path
from app.media.processing import versioned_thumb_name


def _create(username, admin=False):
    assert insert_user(username, generate_password_hash('password123'))
    uid = fetch_user_for_login(username)[0]
    set_user_admin(uid, admin)
    return uid


def _image(uid, color='red', private=False):
    """Ingest a small PNG; returns the media row."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(buf, format='PNG')
    fd, tmp = tempfile.mkstemp(dir=Config.UPLOAD_FOLDER, suffix='.png')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(buf.getvalue())
    status, digest = ingest.ingest_file(tmp, 'png', 'x', uid, private)
    assert status == 'ok'
    return get_media_by_id(get_media_by_hash(digest)[0])


def _files(row):
    return media_path(row['filepath']), thumb_path(row['thumb'])


def _dir_listing():
    return sorted(os.listdir(Config.UPLOAD_FOLDER)), sorted(os.listdir(Config.THUMB_FOLDER))


# ── Privacy flip ──────────────────────────────────────────────────────────
def test_a_failed_privacy_flip_removes_what_it_encrypted(app, user, monkeypatch):
    row = _image(user[0])
    before = _dir_listing()
    real = ingest.encrypt_file
    calls = []

    def encrypt_then_fail(src, dst, key):
        calls.append(dst)
        if len(calls) == 2:          # the thumbnail, after the media file
            raise OSError('disk full')
        real(src, dst, key)
    monkeypatch.setattr(ingest, 'encrypt_file', encrypt_then_fail)

    assert ingest.set_media_privacy(row['id'], True, user[0]) is False
    assert not os.path.exists(calls[0])
    assert _dir_listing() == before
    assert not get_media_by_id(row['id'])['is_private']


def test_a_privacy_flip_the_row_refuses_removes_both_new_files(app, user, monkeypatch):
    row = _image(user[0])
    before = _dir_listing()
    monkeypatch.setattr(ingest, 'set_media_paths_privacy', lambda *a: False)

    assert ingest.set_media_privacy(row['id'], True, user[0]) is False
    assert _dir_listing() == before
    assert all(os.path.exists(p) for p in _files(row))


# ── Thumbnail re-renders ──────────────────────────────────────────────────
def _write_then_fail(src, dst, *key):
    with open(dst, 'wb') as fh:
        fh.write(b'half a png')
    raise RuntimeError('decoder crashed')


def test_a_failed_image_rerender_removes_the_half_written_thumb(app, user, monkeypatch):
    row = _image(user[0])
    monkeypatch.setattr(thumb_repair, '_render', _write_then_fail)

    new, reason = thumb_repair.rerender_image_thumb(
        row['id'], row['filepath'], row['thumb'], None)

    assert new is None and reason.startswith('render failed: RuntimeError')
    assert not os.path.exists(thumb_path(versioned_thumb_name(row['thumb'])))
    assert os.path.exists(thumb_path(row['thumb']))


def test_a_failed_video_render_removes_the_half_written_thumb(app, tmp_path, monkeypatch):
    src = tmp_path / 'clip.mp4'
    src.write_bytes(b'not really a video')
    placeholder = os.path.join(Config.THUMB_FOLDER, 'clip.png')
    open(placeholder, 'wb').close()
    monkeypatch.setattr(tasks, '_render', _write_then_fail)

    with pytest.raises(RuntimeError):
        tasks._process(str(src), placeholder, None)

    assert not os.path.exists(os.path.join(Config.THUMB_FOLDER,
                                           versioned_thumb_name('clip.png')))
    assert os.path.exists(placeholder)


# ── Deleting a user ───────────────────────────────────────────────────────
@pytest.fixture()
def admin_client(client):
    _create('root', admin=True)
    resp = client.post('/login', data={'username': 'root', 'password': 'password123'})
    assert resp.status_code == 302
    return client


def test_reassigning_keyless_private_media_deletes_it_and_its_files(admin_client):
    """Private files whose owner has no key can't be re-encrypted for
    anyone, so the reassignment drops them, files included."""
    carol, dave = _create('carol'), _create('dave')
    row = _image(carol, private=True)
    with db_conn() as conn:
        conn.execute("UPDATE users.users SET enc_key = NULL WHERE id = %s", (carol,))

    resp = admin_client.post(f'/admin/users/{carol}/delete',
                             data={'media_action': f'reassign:{dave}'})

    assert resp.status_code == 302 and fetch_user_by_id(carol) is None
    assert get_media_by_id(row['id']) is None
    assert not any(os.path.exists(p) for p in _files(row))


def test_deleting_a_user_removes_their_staged_thumbnails(admin_client):
    carol = _create('carol')
    sid = staging_db.insert_staging_row(carol, 'inbox.png', 10, 1.0)
    staging_db.update_staging_processed(sid, 'h-staged', 'staged.png', 'image', 'ready')
    os.makedirs(Config.STAGING_THUMB_DIR, exist_ok=True)
    thumb = staging_db.staging_thumb_path('staged.png')
    open(thumb, 'wb').close()

    resp = admin_client.post(f'/admin/users/{carol}/delete',
                             data={'media_action': 'delete'})

    assert resp.status_code == 302
    assert not os.path.exists(thumb)
