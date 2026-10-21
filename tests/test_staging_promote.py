"""Promote/reject flow: staging queue → permanent index."""
import io
import os

import pytest
from PIL import Image

from app.config import Config


def _real_jpeg():
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), (200, 30, 30)).save(buf, format='JPEG')
    return buf.getvalue()


@pytest.fixture()
def ready_item(app, logged_in):
    """A staged file scanned to 'ready'; returns its staging row."""
    from app.media.staging import scan_once
    from app.media.staging_db import list_staging
    user_dir = os.path.join(Config.STAGING_DIR, str(logged_in))
    os.makedirs(user_dir, exist_ok=True)
    with open(os.path.join(user_dir, 'photo.jpg'), 'wb') as f:
        f.write(_real_jpeg())
    scan_once()
    scan_once()
    row = list_staging(logged_in)[0]
    assert row['status'] == 'ready'
    return row


def test_promote_private(client, logged_in, ready_item):
    from app.media.db import get_media_by_id, get_media_ids
    from app.media.staging_db import list_staging

    resp = client.post(f"/api/staging/{ready_item['id']}/promote",
                       data={'tags': 'cat,test', 'private': '1'})
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'ok'

    ids = get_media_ids(viewer_id=logged_in)
    assert len(ids) == 1
    row = get_media_by_id(ids[0])
    assert row['is_private'] is True
    assert row['filepath'].endswith('.enc')   # encrypted file
    assert 'cat' in row['tags'] and 'test' in row['tags']
    assert os.path.isfile(os.path.join(Config.UPLOAD_FOLDER, row['filepath']))

    # Gone from queue, staging dir, and staging thumbs.
    assert list_staging(logged_in) == []
    assert not os.path.exists(
        os.path.join(Config.STAGING_DIR, str(logged_in), 'photo.jpg'))
    assert not os.path.exists(
        os.path.join(Config.STAGING_THUMB_DIR, ready_item['thumb']))


def test_promote_public(client, logged_in, ready_item):
    from app.media.db import get_media_by_id, get_media_ids
    resp = client.post(f"/api/staging/{ready_item['id']}/promote",
                       data={'tags': 'dog'})
    assert resp.status_code == 200
    row = get_media_by_id(get_media_ids(viewer_id=logged_in)[0])
    assert row['is_private'] is False
    assert not row['filepath'].endswith('.enc')


def test_promote_raced_duplicate(client, logged_in, ready_item):
    from app.media.db import insert_media
    from app.media.staging_db import list_staging
    # Same hash lands in media between scan and promote.
    insert_media('winner.jpg', 'winner.png', 't', ready_item['filehash'], logged_in)
    resp = client.post(f"/api/staging/{ready_item['id']}/promote",
                       data={'tags': 'x'})
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'duplicate'
    assert list_staging(logged_in) == []


def test_promote_vanished_file(client, logged_in, ready_item):
    from app.media.staging_db import list_staging
    os.remove(os.path.join(Config.STAGING_DIR, str(logged_in), 'photo.jpg'))
    resp = client.post(f"/api/staging/{ready_item['id']}/promote",
                       data={'tags': 'x'})
    assert resp.status_code == 410
    assert list_staging(logged_in) == []


def test_promote_of_still_uploading_file_refused(client, logged_in, ready_item):
    """A file that changed after being scanned (stalled DAV transfer that
    resumed) must not be promoted — moving it would truncate the upload."""
    from app.media.staging import scan_once
    from app.media.staging_db import list_staging
    path = os.path.join(Config.STAGING_DIR, str(logged_in), 'photo.jpg')
    with open(path, 'ab') as f:
        f.write(b'transfer-resumed-bytes')
    resp = client.post(f"/api/staging/{ready_item['id']}/promote",
                       data={'tags': 'x'})
    assert resp.status_code == 409
    # Row is handed back; the scanner re-processes it and promote then works.
    scan_once()
    scan_once()
    row = list_staging(logged_in)[0]
    assert row['status'] == 'ready'
    assert row['filehash'] != ready_item['filehash']
    resp = client.post(f"/api/staging/{row['id']}/promote", data={'tags': 'x'})
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'ok'


def test_promote_foreign_row_404(client, logged_in, ready_item):
    # Hand the row to a real second user (the Postgres FK on
    # staging.user_id forbids pointing it at a nonexistent id).
    from werkzeug.security import generate_password_hash
    from app.auth.db import insert_user, fetch_user_for_login
    from app.db_utils import db_conn
    insert_user('mallory', generate_password_hash('password456'))
    other_uid = fetch_user_for_login('mallory')[0]
    with db_conn() as conn:
        conn.cursor().execute(
            "UPDATE media.staging SET user_id=%s WHERE id=%s",
            (other_uid, ready_item['id']))
        conn.commit()
    resp = client.post(f"/api/staging/{ready_item['id']}/promote",
                       data={'tags': 'x'})
    assert resp.status_code == 404


def test_reject_removes_everything(client, logged_in, ready_item):
    from app.media.staging_db import list_staging
    resp = client.post(f"/api/staging/{ready_item['id']}/reject")
    assert resp.status_code == 200
    assert list_staging(logged_in) == []
    assert not os.path.exists(
        os.path.join(Config.STAGING_DIR, str(logged_in), 'photo.jpg'))
    assert not os.path.exists(
        os.path.join(Config.STAGING_THUMB_DIR, ready_item['thumb']))


def test_queue_page_and_listing(client, logged_in, ready_item):
    page = client.get('/queue')
    assert page.status_code == 200
    data = client.get('/api/staging').get_json()
    assert len(data['items']) == 1
    assert data['items'][0]['status'] == 'ready'
    assert data['items'][0]['thumb_url']
    # Staged thumb and file are served to the owner.
    assert client.get(data['items'][0]['thumb_url']).status_code == 200
    assert client.get(data['items'][0]['file_url']).status_code == 200


def test_staging_urls_require_ownership(app, client, user, ready_item):
    # A second logged-in user must not see alice's staged item.
    from werkzeug.security import generate_password_hash
    from app.auth.db import insert_user
    insert_user('carol', generate_password_hash('carolpass1'))
    client.post('/logout')
    client.post('/login', data={'username': 'carol', 'password': 'carolpass1'})
    assert client.get(f"/staging/thumb/{ready_item['id']}").status_code == 404
    assert client.get(f"/staging/file/{ready_item['id']}").status_code == 404
    assert client.get('/api/staging').get_json()['items'] == []


def test_dav_password_generate_and_revoke(client, logged_in):
    from app.auth.db import fetch_user_for_dav
    resp = client.post('/api/dav/password')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['password'] and data['dav_url'].endswith('/dav/')
    assert fetch_user_for_dav('alice')[1] is not None
    resp = client.post('/api/dav/password/revoke')
    assert resp.status_code == 200
    assert fetch_user_for_dav('alice')[1] is None
