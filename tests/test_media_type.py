"""The media type is the stored `ftype`, wherever it is shown.

insert_media stores the type once, from the file name with any '.enc'
stripped. Every reader uses that value instead of working it out again
from the name: the gallery API, the lightbox metadata, the first gallery
page (rendered by Jinja) and the duplicate finder's per-type counts. These
pin that they agree, private files included.
"""
from app.admin import dupes
from app.media.db import get_media_by_hash, insert_media

ROWS = (('a.png', 'image', False), ('b.gif', 'gif', False),
        ('c.mp4', 'video', False), ('d.webm.enc', 'video', True))


def _seed(uid):
    """{media id: expected type}."""
    expected = {}
    for name, ftype, private in ROWS:
        insert_media(name, f'{name}.jpg', 'x', f'hash-{name}', uid, private)
        expected[get_media_by_hash(f'hash-{name}')[0]] = ftype
    return expected


def test_the_gallery_api_reports_the_stored_type(client, logged_in):
    expected = _seed(logged_in)
    media = client.get('/api/media').get_json()['media']
    assert {m['id']: m['type'] for m in media} == expected


def test_the_lightbox_metadata_reports_the_stored_type(client, logged_in):
    expected = _seed(logged_in)
    for mid, ftype in expected.items():
        assert client.get(f'/mediadata/{mid}').get_json()['type'] == ftype


def test_the_first_gallery_page_badges_each_card(client, logged_in):
    _seed(logged_in)
    html = client.get('/').get_data(as_text=True)
    for ftype, n in (('image', 1), ('gif', 1), ('video', 2)):
        assert html.count(f'class="type-badge {ftype}">{ftype}</span>') == n


def test_the_duplicate_finder_counts_public_media_per_type(app, user):
    _seed(user[0])
    assert dupes.library_scale() == {'image': 1, 'gif': 1, 'video': 1}
