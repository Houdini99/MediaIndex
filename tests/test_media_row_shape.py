"""Media reads return mappings with stable names, not positional tuples.

Nothing may depend on the physical column order of media.media: with
`SELECT *` tuples indexed by position across routes, templates and the
admin panel, adding a column in the wrong place would silently corrupt
the JSON API instead of raising. These pin that contract.
"""

from werkzeug.security import generate_password_hash

from app.auth.db import insert_user
from app.media.db import (
    MEDIA_COLUMNS, get_media, get_media_by_id, get_media_by_ids,
    get_media_ids, insert_media,
)

EXPECTED = {'id', 'filepath', 'thumb', 'tags', 'filehash', 'uploaded_by',
            'upload_date', 'is_private', 'ftype'}


def _seed(app):
    insert_user('shape', generate_password_hash('password123'))
    from app.media.db import get_user_id_by_username
    uid = get_user_id_by_username('shape')
    insert_media('a.png', 'a.png', 'shapetest', 'hash-a', uid, False)
    return uid


def test_column_list_matches_the_documented_names():
    assert {c.strip() for c in MEDIA_COLUMNS.split(',')} == EXPECTED


def test_get_media_by_id_returns_named_fields(app):
    _seed(app)
    row = get_media_by_id(get_media_ids()[0])
    assert EXPECTED <= set(row)
    assert row['filepath'] == 'a.png'
    assert row['is_private'] is False


def test_get_media_adds_the_uploader_name(app):
    """A named key, so every read returns rows of the same shape whichever
    function produced them."""
    _seed(app)
    row = get_media()[0]
    assert row['uploader'] == 'shape'
    assert EXPECTED <= set(row)


def test_get_media_by_ids_returns_named_fields(app):
    _seed(app)
    rows = get_media_by_ids(get_media_ids())
    assert rows and EXPECTED <= set(rows[0])


def test_rows_are_not_indexable_by_position(app):
    """The point of the change: a positional read must fail loudly now
    rather than quietly return the wrong column after a schema edit."""
    _seed(app)
    row = get_media_by_id(get_media_ids()[0])
    try:
        row[0]
    except KeyError:
        return
    raise AssertionError("row still supports positional access")
