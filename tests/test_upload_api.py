"""POST /api/upload — the JSON upload endpoint the Discord bot uses instead
of scraping CSRF tokens and flash markup out of the web form.

Covers app-password authentication (and its refusal modes), the per-file
result contract, delegated attribution, and the duplicate report — which
must name the colliding item for public media and stay silent about tags
and ownership for private media.
"""
import io

from PIL import Image
from werkzeug.security import generate_password_hash

from app.auth.db import (
    fetch_user_for_login,
    hash_dav_password,
    insert_user,
    set_dav_password_hash,
)

TOKEN = 'app-password-token-123'


def _png_bytes(color):
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buf, format='PNG')
    return buf.getvalue()


def _basic(username, password=TOKEN):
    import base64
    raw = base64.b64encode(f'{username}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


def _account(username, with_token=True):
    insert_user(username, generate_password_hash('password123'))
    uid = fetch_user_for_login(username)[0]
    if with_token:
        set_dav_password_hash(uid, hash_dav_password(TOKEN))
    return uid


def _post(client, data, headers=None, name='pic.png', **fields):
    payload = {'file': (io.BytesIO(data), name)}
    payload.update(fields)
    return client.post('/api/upload', data=payload, headers=headers,
                       content_type='multipart/form-data')


# ── Authentication ────────────────────────────────────────────────────────
def test_rejects_anonymous(client, app):
    _account('Bot')
    resp = _post(client, _png_bytes('red'))
    assert resp.status_code == 401
    assert 'Basic' in resp.headers.get('WWW-Authenticate', '')


def test_rejects_wrong_app_password(client, app):
    _account('Bot')
    resp = _post(client, _png_bytes('red'), headers=_basic('Bot', 'nope'))
    assert resp.status_code == 401


def test_rejects_account_without_app_password(client, app):
    """The login password must not work here — only the app password."""
    _account('Bot', with_token=False)
    resp = _post(client, _png_bytes('red'), headers=_basic('Bot', 'password123'))
    assert resp.status_code == 401


def test_session_cookie_is_not_accepted(client, logged_in, app):
    """The endpoint is CSRF-exempt, which is only safe because it ignores
    ambient browser credentials."""
    resp = _post(client, _png_bytes('red'))
    assert resp.status_code == 401


# ── Upload results ────────────────────────────────────────────────────────
def test_upload_reports_ok_with_filehash(client, app):
    uid = _account('Bot')
    resp = _post(client, _png_bytes('red'), headers=_basic('Bot'), tags='cats')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['uploaded_by'] == 'Bot'
    result = body['results'][0]
    assert result['status'] == 'ok'
    assert result['filename'] == 'pic.png'
    assert len(result['filehash']) == 64

    from app.media.db import get_media_by_hash
    row = get_media_by_hash(result['filehash'])
    assert row[4] == uid
    assert 'cats' in row[3]


def test_unsupported_type_is_an_error_not_a_duplicate(client, app):
    _account('Bot')
    resp = _post(client, b'not an image', headers=_basic('Bot'), name='notes.txt')
    result = resp.get_json()['results'][0]
    assert result['status'] == 'error'
    assert result['detail'] == 'unsupported file type'
    assert result['filehash'] is None


def test_missing_file_is_a_400(client, app):
    _account('Bot')
    resp = client.post('/api/upload', data={'tags': 'cats'},
                       headers=_basic('Bot'),
                       content_type='multipart/form-data')
    assert resp.status_code == 400


def test_delegated_attribution_applies(client, app):
    _account('Bot')
    target = _account('bob', with_token=False)
    resp = _post(client, _png_bytes('blue'), headers=_basic('Bot'),
                 uploaded_for='bob')
    body = resp.get_json()
    assert body['uploaded_by'] == 'bob'
    from app.media.db import get_media_by_hash
    assert get_media_by_hash(body['results'][0]['filehash'])[4] == target


def test_uploaded_for_ignored_for_undelegated_account(client, app):
    """'alice' is not in DELEGATED_UPLOADERS — ownership stays with her."""
    alice = _account('alice')
    _account('bob', with_token=False)
    resp = _post(client, _png_bytes('green'), headers=_basic('alice'),
                 uploaded_for='bob')
    body = resp.get_json()
    assert body['uploaded_by'] == 'alice'
    from app.media.db import get_media_by_hash
    assert get_media_by_hash(body['results'][0]['filehash'])[4] == alice


def test_private_delegated_upload_belongs_to_the_named_user(client, app):
    """The Discord bot's DM ingest: private, and owned by the sender."""
    _account('Bot')
    bob = _account('bob', with_token=False)
    resp = _post(client, _png_bytes('teal'), headers=_basic('Bot'),
                 uploaded_for='bob', private='1')
    body = resp.get_json()
    assert body['results'][0]['status'] == 'ok'
    assert body['uploaded_by'] == 'bob'
    from app.media.db import get_media_by_hash
    row = get_media_by_hash(body['results'][0]['filehash'])
    assert row[4] == bob and row[5] == 1 and row[1].endswith('.enc')


def test_private_upload_for_an_unknown_user_is_refused(client, app):
    """The usual fallback — unknown name, the uploader keeps it — would
    file someone's private media, encrypted, in the bot's own library."""
    _account('Bot')
    resp = _post(client, _png_bytes('maroon'), headers=_basic('Bot'),
                 uploaded_for='ghost', private='1')
    result = resp.get_json()['results'][0]
    assert result['status'] == 'error'
    assert 'ghost' in result['detail']
    from app.media.db import get_all_media_paths
    assert get_all_media_paths() == []


def test_public_upload_for_an_unknown_user_still_falls_back(client, app):
    """The refusal is for private items only; a public one keeps the old
    "a mapping typo never drops media" behaviour."""
    bot = _account('Bot')
    resp = _post(client, _png_bytes('olive'), headers=_basic('Bot'),
                 uploaded_for='ghost')
    body = resp.get_json()
    assert body['results'][0]['status'] == 'ok'
    from app.media.db import get_media_by_hash
    assert get_media_by_hash(body['results'][0]['filehash'])[4] == bot


# ── Duplicate feedback ────────────────────────────────────────────────────
def test_duplicate_names_the_colliding_item(client, app):
    _account('Bot')
    data = _png_bytes('red')
    first = _post(client, data, headers=_basic('Bot'), tags='cats,funny')
    assert first.get_json()['results'][0]['status'] == 'ok'

    again = _post(client, data, headers=_basic('Bot'), name='copy.png')
    result = again.get_json()['results'][0]
    assert result['status'] == 'duplicate'
    assert result['filehash'] == first.get_json()['results'][0]['filehash']
    dup = result['duplicate_of']
    assert dup['is_private'] is False
    assert dup['uploaded_by'] == 'Bot'
    assert 'cats' in dup['tags'] and 'funny' in dup['tags']
    assert dup['filepath'].startswith(result['filehash'])
    assert isinstance(dup['id'], int)


def test_duplicate_of_a_private_item_leaks_nothing(client, app):
    """Re-posting a file that happens to collide with someone's private
    upload must not reveal its tags or its owner in a Discord channel."""
    _account('Bot')
    alice = _account('alice')
    data = _png_bytes('purple')
    _post(client, data, headers=_basic('alice'), tags='secret,diary', private='1')

    resp = _post(client, data, headers=_basic('Bot'))
    result = resp.get_json()['results'][0]
    assert result['status'] == 'duplicate'
    dup = result['duplicate_of']
    assert dup['is_private'] is True
    assert set(dup) == {'id', 'is_private'}
    assert alice  # the owner exists but is deliberately not named


def test_private_flag_encrypts_the_upload(client, app):
    _account('Bot')
    resp = _post(client, _png_bytes('orange'), headers=_basic('Bot'), private='1')
    fh = resp.get_json()['results'][0]['filehash']
    from app.media.db import get_media_by_hash
    row = get_media_by_hash(fh)
    assert row[5] == 1
    assert row[1].endswith('.enc')


# ── The automatic type tag ────────────────────────────────────────────────
def _stored_tags(resp):
    from app.media.db import get_media_by_hash
    from app.media.tagging import tag_list
    return tag_list(get_media_by_hash(resp.get_json()['results'][0]['filehash'])[3])


def test_the_type_tag_is_added_when_missing(client, app):
    _account('Bot')
    resp = _post(client, _png_bytes('navy'), headers=_basic('Bot'), tags='cats')
    assert _stored_tags(resp) == ['cats', 'image', 'user:Bot']


def test_a_typed_type_tag_is_not_added_twice(client, app):
    _account('Bot')
    resp = _post(client, _png_bytes('teal'), headers=_basic('Bot'),
                 tags='image,cats')
    assert _stored_tags(resp) == ['image', 'cats', 'user:Bot']


def test_the_type_tag_check_ignores_case(client, app):
    """Tag search ignores case, so `Image` and `image` would be one tag twice."""
    _account('Bot')
    resp = _post(client, _png_bytes('olive'), headers=_basic('Bot'),
                 tags='Image')
    assert _stored_tags(resp) == ['Image', 'user:Bot']
