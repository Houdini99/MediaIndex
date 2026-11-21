"""The JSON answers the front end relies on to work without page reloads —
which on a phone is the difference between keeping your place and losing
every page infinite scroll had loaded:

  - the upload page posts with XHR to show a progress bar, and renders the
    results itself;
  - the gallery edits tags in place and redraws the card from the stored,
    normalized tags;
  - the lightbox offers Edit and Delete only to whoever may use them.

The plain form posts these routes always answered are unchanged.
"""
import io

from PIL import Image
from werkzeug.security import generate_password_hash

from app.auth.db import fetch_user_for_login, insert_user, set_user_admin
from app.media.db import get_all_media_paths, get_media_by_id

XHR = {'X-Requested-With': 'XMLHttpRequest'}


def _png(color):
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buf, format='PNG')
    return buf.getvalue()


def _upload(client, data, name='pic.png', headers=None):
    return client.post('/upload',
                       data={'files': (io.BytesIO(data), name), 'tags_0': 'test'},
                       content_type='multipart/form-data', headers=headers)


def _login_as(client, username, admin=False):
    insert_user(username, generate_password_hash('password123'))
    if admin:
        set_user_admin(fetch_user_for_login(username)[0], True)
    client.post('/logout')
    client.post('/login', data={'username': username, 'password': 'password123'})


# ── Upload ────────────────────────────────────────────────────────────────
def test_scripted_upload_answers_with_its_results(client, logged_in):
    resp = _upload(client, _png('red'), headers=XHR)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['counts'] == {'ok': 1, 'duplicate': 0, 'error': 0}
    assert body['messages'] == [{'category': 'success', 'text': '✅ Successful: pic.png'}]
    # Answered, not flashed: a flash would surface on some later page.
    # (The login's own "Welcome" may still be waiting; that one is fine.)
    with client.session_transaction() as sess:
        assert not any('Successful' in text for _, text in sess.get('_flashes', []))


def test_scripted_upload_reports_a_duplicate(client, logged_in):
    data = _png('blue')
    _upload(client, data, headers=XHR)
    body = _upload(client, data, name='again.png', headers=XHR).get_json()
    assert body['counts'] == {'ok': 0, 'duplicate': 1, 'error': 0}
    assert body['messages'] == [{'category': 'warning', 'text': '⚠️ Duplicates: again.png'}]
    assert len(get_all_media_paths()) == 1


def test_form_upload_still_flashes_and_redirects(client, logged_in):
    resp = _upload(client, _png('green'))
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/upload')
    assert 'Successful'.encode() in client.get('/upload').data


# ── Tag edit ──────────────────────────────────────────────────────────────
def test_scripted_edit_returns_the_stored_tags(client, logged_in):
    _upload(client, _png('teal'))
    mid = get_all_media_paths()[0][0]
    resp = client.post(f'/edit/{mid}', data={'tags': ' cats ,dogs,, birds '}, headers=XHR)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['updated'] is True
    # The card is redrawn from what was stored, not from what was typed.
    assert body['tags'] == 'cats,dogs,birds'
    assert body['tags'] == get_media_by_id(mid)['tags']


def test_scripted_edit_of_someone_elses_media_is_refused(client, logged_in):
    _upload(client, _png('navy'))
    mid = get_all_media_paths()[0][0]
    before = get_media_by_id(mid)['tags']
    _login_as(client, 'mallory')
    resp = client.post(f'/edit/{mid}', data={'tags': 'vandalized'}, headers=XHR)
    assert resp.status_code == 403
    assert resp.get_json() == {'updated': False, 'tags': None}
    assert get_media_by_id(mid)['tags'] == before


def test_form_edit_still_flashes_and_redirects(client, logged_in):
    _upload(client, _png('olive'))
    mid = get_all_media_paths()[0][0]
    resp = client.post(f'/edit/{mid}', data={'tags': 'plain'})
    assert resp.status_code == 302
    assert 'successfully updated'.encode() in client.get('/').data


# ── Lightbox permissions ──────────────────────────────────────────────────
def test_mediadata_says_who_may_edit(client, logged_in):
    _upload(client, _png('pink'))
    mid = get_all_media_paths()[0][0]
    assert client.get(f'/mediadata/{mid}').get_json()['can_edit'] is True    # owner

    _login_as(client, 'mallory')
    assert client.get(f'/mediadata/{mid}').get_json()['can_edit'] is False   # another user

    _login_as(client, 'root', admin=True)
    assert client.get(f'/mediadata/{mid}').get_json()['can_edit'] is True    # an admin
