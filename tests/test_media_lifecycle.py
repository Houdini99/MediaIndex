"""Upload dedup (incl. the TOCTOU race cleanup), delete-path resolution,
and the legacy-path basename migration."""
import io
import os

import pytest

from PIL import Image

from app.config import Config
from app.db_utils import db_conn
from app.media.db import (
    insert_media, delete_media, get_all_media_paths,
)


def _png_bytes(color):
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buf, format='PNG')
    return buf.getvalue()


def _upload(client, data, name='pic.png', private=False):
    payload = {'files': (io.BytesIO(data), name), 'tags_0': 'test'}
    if private:
        payload['private_0'] = '1'
    return client.post('/upload', data=payload,
                       content_type='multipart/form-data', follow_redirects=True)


def _disk_files():
    # Files only: THUMB_FOLDER also contains the staging/ thumbs subdirectory.
    def _files(folder):
        return sorted(e.name for e in os.scandir(folder) if e.is_file())
    return _files(Config.UPLOAD_FOLDER), _files(Config.THUMB_FOLDER)


# ── Upload ────────────────────────────────────────────────────────────────
def test_upload_stores_basenames(client, logged_in):
    resp = _upload(client, _png_bytes('red'))
    assert resp.status_code == 200
    rows = get_all_media_paths()
    assert len(rows) == 1
    _, fp, th = rows[0]
    assert '/' not in fp and '/' not in th
    assert os.path.exists(os.path.join(Config.UPLOAD_FOLDER, fp))
    assert os.path.exists(os.path.join(Config.THUMB_FOLDER, th))


def test_upload_duplicate_detected(client, logged_in):
    data = _png_bytes('blue')
    _upload(client, data)
    resp = _upload(client, data, name='same-content.png')
    assert 'Duplicates'.encode() in resp.data
    assert len(get_all_media_paths()) == 1
    media, thumbs = _disk_files()
    assert len(media) == 1 and len(thumbs) == 1


def test_upload_dedup_race_cleans_files(client, logged_in, monkeypatch):
    """If hash_exists misses but the INSERT loses the UNIQUE race, the
    already-written media file and thumb must be removed."""
    data = _png_bytes('green')
    _upload(client, data)
    files_before = _disk_files()
    # Simulate the TOCTOU window: the pre-check says "new" but the row exists.
    monkeypatch.setattr('app.media.ingest.hash_exists', lambda fh: False)
    resp = _upload(client, data, name='racer.png')
    assert 'Duplicates'.encode() in resp.data
    assert len(get_all_media_paths()) == 1
    assert _disk_files() == files_before


def test_upload_unicode_filename(client, logged_in):
    resp = _upload(client, _png_bytes('gold'), name='лягушка😀.png')
    assert 'Successful'.encode('utf-8') in resp.data
    assert len(get_all_media_paths()) == 1


def test_upload_disallowed_extension_rejected(client, logged_in):
    resp = _upload(client, b'not really media', name='evil.exe')
    assert 'Error'.encode() in resp.data
    assert len(get_all_media_paths()) == 0
    media, thumbs = _disk_files()
    assert media == [] and thumbs == []


# ── Delete ────────────────────────────────────────────────────────────────
def test_delete_removes_files(client, logged_in):
    _upload(client, _png_bytes('cyan'))
    rows = get_all_media_paths()
    mid = rows[0][0]
    resp = client.post(f'/delete/{mid}', follow_redirects=True)
    assert resp.status_code == 200
    assert get_all_media_paths() == []
    media, thumbs = _disk_files()
    assert media == [] and thumbs == []


def test_delete_legacy_relative_path_row(app, user):
    """Pre-migration rows stored 'meme_files/x.jpg' or absolute paths; the
    file must still be found and removed via the configured folders."""
    uid = user[0]
    real = os.path.join(Config.UPLOAD_FOLDER, 'legacy.jpg')
    thumb = os.path.join(Config.THUMB_FOLDER, 'legacy.png')
    open(real, 'wb').write(b'x')
    open(thumb, 'wb').write(b'x')
    assert insert_media('meme_files/legacy.jpg', '/app/thumbnails/legacy.png',
                        'old', 'hash-legacy', uid)
    # insert_media normalizes to basenames, so simulate a true legacy row
    with db_conn() as conn:
        conn.execute("UPDATE media.media SET filepath='meme_files/legacy.jpg', "
                     "thumb='/app/thumbnails/legacy.png'")
        conn.commit()
    mid = get_all_media_paths()[0][0]
    assert delete_media(mid, uid)
    assert not os.path.exists(real)
    assert not os.path.exists(thumb)


def test_delete_requires_owner(client, logged_in, app):
    from app.auth.db import insert_user, fetch_user_for_login
    from werkzeug.security import generate_password_hash
    _upload(client, _png_bytes('pink'))
    mid = get_all_media_paths()[0][0]
    insert_user('mallory', generate_password_hash('password123'))
    other = fetch_user_for_login('mallory')[0]
    assert not delete_media(mid, other)
    assert len(get_all_media_paths()) == 1


def test_admin_can_delete_and_edit_others_media(client, logged_in, app):
    from app.auth.db import insert_user, fetch_user_for_login, set_user_admin
    from app.media.db import get_media_by_id, update_media_tags
    from werkzeug.security import generate_password_hash
    _upload(client, _png_bytes('teal'))
    mid = get_all_media_paths()[0][0]
    insert_user('root', generate_password_hash('password123'))
    admin = fetch_user_for_login('root')[0]
    set_user_admin(admin, True)
    assert update_media_tags(mid, 'moderated', admin, is_admin=True)
    assert get_media_by_id(mid)['tags'] == 'moderated'
    assert delete_media(mid, admin, is_admin=True)
    assert get_all_media_paths() == []


def test_admin_routes_manage_others_media(client, logged_in, app):
    """/edit and /delete accept a logged-in admin who is not the owner."""
    from app.auth.db import insert_user, fetch_user_for_login, set_user_admin
    from app.media.db import get_media_by_id
    from werkzeug.security import generate_password_hash
    _upload(client, _png_bytes('navy'))      # owned by the fixture user
    mid = get_all_media_paths()[0][0]
    insert_user('root', generate_password_hash('password123'))
    set_user_admin(fetch_user_for_login('root')[0], True)
    client.post('/logout')
    client.post('/login', data={'username': 'root', 'password': 'password123'})
    resp = client.post(f'/edit/{mid}', data={'tags': 'moderated'}, follow_redirects=True)
    assert 'successfully updated'.encode() in resp.data
    assert get_media_by_id(mid)['tags'] == 'moderated'
    client.post(f'/delete/{mid}', follow_redirects=True)
    assert get_all_media_paths() == []


def test_bulk_delete_removes_files(client, logged_in):
    _upload(client, _png_bytes('red'))
    _upload(client, _png_bytes('lime'), name='pic2.png')
    ids = [r[0] for r in get_all_media_paths()]
    resp = client.post('/delete-bulk', json={'ids': ids})
    assert resp.status_code == 200
    assert resp.get_json() == {'deleted': 2, 'failed': 0}
    assert get_all_media_paths() == []
    media, thumbs = _disk_files()
    assert media == [] and thumbs == []


def test_bulk_delete_skips_others_media(client, logged_in, app):
    """Non-owned ids are counted as failed and their files stay on disk."""
    from app.auth.db import insert_user
    from werkzeug.security import generate_password_hash
    _upload(client, _png_bytes('plum'))       # owned by the fixture user
    mid = get_all_media_paths()[0][0]
    insert_user('mallory', generate_password_hash('password123'))
    client.post('/logout')
    client.post('/login', data={'username': 'mallory', 'password': 'password123'})
    resp = client.post('/delete-bulk', json={'ids': [mid, 999999]})
    assert resp.status_code == 200
    assert resp.get_json() == {'deleted': 0, 'failed': 2}
    assert len(get_all_media_paths()) == 1


def test_bulk_delete_rejects_bad_payloads(client, logged_in):
    assert client.post('/delete-bulk', json={'ids': []}).status_code == 400
    assert client.post('/delete-bulk', json={'ids': 'nope'}).status_code == 400
    assert client.post('/delete-bulk', json={'ids': ['x']}).status_code == 400
    assert client.post('/delete-bulk', json={}).status_code == 400


# ── Bulk tag edit ─────────────────────────────────────────────────────────
def test_bulk_edit_adds_and_removes_tags(client, logged_in):
    from app.media.db import get_media_by_id
    _upload(client, _png_bytes('coral'))
    _upload(client, _png_bytes('olive'), name='pic2.png')
    ids = [r[0] for r in get_all_media_paths()]
    resp = client.post('/edit-bulk', json={'ids': ids, 'add': 'meme, cat', 'remove': 'test'})
    assert resp.status_code == 200
    assert resp.get_json() == {'updated': 2, 'failed': 0}
    for mid in ids:
        tags = [t.strip() for t in get_media_by_id(mid)['tags'].split(',')]
        assert 'meme' in tags and 'cat' in tags and 'test' not in tags


def test_bulk_edit_deduplicates_added_tags(client, logged_in):
    from app.media.db import get_media_by_id
    _upload(client, _png_bytes('wheat'))     # uploads carry the 'test' tag
    mid = get_all_media_paths()[0][0]
    resp = client.post('/edit-bulk', json={'ids': [mid], 'add': 'test, test, new'})
    assert resp.get_json() == {'updated': 1, 'failed': 0}
    tags = [t.strip() for t in get_media_by_id(mid)['tags'].split(',')]
    assert tags.count('test') == 1 and tags.count('new') == 1


def test_bulk_edit_skips_others_media(client, logged_in, app):
    from app.auth.db import insert_user
    from app.media.db import get_media_by_id
    from werkzeug.security import generate_password_hash
    _upload(client, _png_bytes('khaki'))
    mid = get_all_media_paths()[0][0]
    before = get_media_by_id(mid)['tags']
    insert_user('mallory', generate_password_hash('password123'))
    client.post('/logout')
    client.post('/login', data={'username': 'mallory', 'password': 'password123'})
    resp = client.post('/edit-bulk', json={'ids': [mid], 'add': 'defaced'})
    assert resp.get_json() == {'updated': 0, 'failed': 1}
    assert get_media_by_id(mid)['tags'] == before


def test_bulk_edit_rejects_bad_payloads(client, logged_in):
    _upload(client, _png_bytes('azure'))
    mid = get_all_media_paths()[0][0]
    assert client.post('/edit-bulk', json={'ids': [], 'add': 'x'}).status_code == 400
    assert client.post('/edit-bulk', json={'ids': [mid]}).status_code == 400
    assert client.post('/edit-bulk', json={'ids': [mid], 'add': ' , '}).status_code == 400
    assert client.post('/edit-bulk', json={'ids': [mid], 'add': 42}).status_code == 400


# ── Bulk privacy flip ─────────────────────────────────────────────────────
def test_privacy_flip_round_trip(client, logged_in):
    from app.media.db import get_media_by_id
    data = _png_bytes('magenta')
    _upload(client, data)
    mid = get_all_media_paths()[0][0]

    # no-op flip to the current state counts as updated
    resp = client.post('/privacy-bulk', json={'ids': [mid], 'private': False})
    assert resp.get_json() == {'updated': 1, 'failed': 0}

    # public → private: row + both files renamed .enc, ciphertext on disk
    resp = client.post('/privacy-bulk', json={'ids': [mid], 'private': True})
    assert resp.get_json() == {'updated': 1, 'failed': 0}
    _, fp, th = get_all_media_paths()[0]
    assert fp.endswith('.enc') and th.endswith('.enc')
    assert get_media_by_id(mid)['is_private'] is True
    media, thumbs = _disk_files()
    assert media == [fp] and thumbs == [th]
    assert data not in open(os.path.join(Config.UPLOAD_FOLDER, fp), 'rb').read()
    # the owner still gets the decrypted original through the serving route
    assert client.get(f'/media/{fp}').data == data

    # private → public: original bytes restored, .enc names gone
    resp = client.post('/privacy-bulk', json={'ids': [mid], 'private': False})
    assert resp.get_json() == {'updated': 1, 'failed': 0}
    _, fp2, th2 = get_all_media_paths()[0]
    assert not fp2.endswith('.enc') and not th2.endswith('.enc')
    assert get_media_by_id(mid)['is_private'] is False
    assert open(os.path.join(Config.UPLOAD_FOLDER, fp2), 'rb').read() == data
    media, thumbs = _disk_files()
    assert media == [fp2] and thumbs == [th2]


def test_privacy_flip_requires_owner(client, logged_in, app):
    from app.auth.db import insert_user
    from werkzeug.security import generate_password_hash
    _upload(client, _png_bytes('salmon'))
    mid = get_all_media_paths()[0][0]
    client.post('/privacy-bulk', json={'ids': [mid], 'private': True})
    _, fp, _ = get_all_media_paths()[0]
    insert_user('mallory', generate_password_hash('password123'))
    client.post('/logout')
    client.post('/login', data={'username': 'mallory', 'password': 'password123'})
    # mallory can neither flip nor fetch the private file
    resp = client.post('/privacy-bulk', json={'ids': [mid], 'private': False})
    assert resp.get_json() == {'updated': 0, 'failed': 1}
    assert get_all_media_paths()[0][1].endswith('.enc')
    assert client.get(f'/media/{fp}').status_code == 404


def test_privacy_bulk_rejects_bad_payloads(client, logged_in):
    _upload(client, _png_bytes('tan'))
    mid = get_all_media_paths()[0][0]
    assert client.post('/privacy-bulk', json={'ids': [mid]}).status_code == 400
    assert client.post('/privacy-bulk', json={'ids': [mid], 'private': 'yes'}).status_code == 400
    assert client.post('/privacy-bulk', json={'ids': [], 'private': True}).status_code == 400


def test_uploaded_files_are_world_readable_like_the_rest(client, logged_in):
    """The upload temp file comes from tempfile (mode 0600) and the rename
    into place kept that, so web and bot uploads were readable by root
    alone while every other stored file was 0644 — host-side backups
    running as the owning user silently skipped them."""
    _upload(client, _png_bytes('olive'))
    _, fp, _ = get_all_media_paths()[0]
    mode = os.stat(os.path.join(Config.UPLOAD_FOLDER, fp)).st_mode & 0o777
    assert mode == 0o644, oct(mode)


def test_upload_is_hashed_in_one_pass(client, logged_in, monkeypatch):
    """The upload is hashed while it is written; reading it back in full
    just to hash it again is exactly what this replaced."""
    import hashlib
    monkeypatch.setattr('app.media.ingest.sha256_file',
                        lambda p: pytest.fail("upload re-read to hash it"))
    data = _png_bytes('maroon')
    resp = _upload(client, data)
    assert 'Successful'.encode() in resp.data
    _, fp, _ = get_all_media_paths()[0]
    assert fp.startswith(hashlib.sha256(data).hexdigest() + '.')


def test_save_stream_hashed_round_trips(tmp_path):
    import hashlib
    from app.media.processing import IO_CHUNK, save_stream_hashed
    data = os.urandom(IO_CHUNK * 2 + 123)          # spans chunk boundaries
    out = tmp_path / 'copy.bin'
    assert save_stream_hashed(io.BytesIO(data), str(out)) == \
        hashlib.sha256(data).hexdigest()
    assert out.read_bytes() == data


def test_an_aborted_upload_leaves_no_temp_file(client, logged_in, monkeypatch):
    def boom(stream, path):
        open(path, 'wb').write(b'partial')
        raise OSError('client went away')
    monkeypatch.setattr('app.media.routes.save_stream_hashed', boom)
    with pytest.raises(OSError):
        _upload(client, _png_bytes('navy'))
    media, _ = _disk_files()
    assert media == []
