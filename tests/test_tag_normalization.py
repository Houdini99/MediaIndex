"""The stored tag string must always have stripped, non-empty parts.

`app.media.tagging` documents this as the invariant that makes the
comma-padded `(',' || tags || ',') ILIKE '%,tag,%'` whole-tag matching in
`_build_where` sound. The read side always honoured it, and so did the two
bulk-write paths, but the two *ordinary* write funnels did not: a form
value of `"alpha, beta"` was stored verbatim, and `beta` — padded to
`,beta,` by the query — could never match `, beta,` in the stored string.
The tag was silently unfindable, with no error anywhere.

`tests/test_build_where.py` covered a spaced value on the read side only,
which is why a green suite never caught this. These tests pin the write
side: insert_media, update_media_tags, and the Redis event that
build_media_event announces (whose docstring promises it mirrors what
insert_media stores).
"""
import io
import re

import pytest
from PIL import Image

from app.db_utils import db_conn
from app.media.db import (
    get_media_by_id,
    get_media_count,
    insert_media,
    normalize_tags,
    update_media_tags,
)
from app.notifications import build_media_event

SPACED = 'alpha, beta,  gamma ,,delta,'
NORMALIZED = ['alpha', 'beta', 'gamma', 'delta']


def _png():
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(buf, format='PNG')
    buf.seek(0)
    return buf


def _find(filehash):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM media.media WHERE filehash=%s", (filehash,))
        return cur.fetchone()[0]


def _stored(mid):
    return get_media_by_id(mid)['tags'].split(',')


def test_a_spaced_tag_is_searchable(user):
    """The bug in one line: this returned 0 before the fix."""
    uid, _username, _password = user
    insert_media('n1.jpg', 'n1.png', 'alpha, beta', 'h-norm-1', uid)
    assert get_media_count(include_tags='beta') == 1


@pytest.mark.parametrize('tag', NORMALIZED)
def test_every_part_of_a_messy_tag_string_is_searchable(user, tag):
    uid, _username, _password = user
    insert_media(f'n2-{tag}.jpg', None, SPACED, f'h-norm-2-{tag}', uid)
    assert get_media_count(include_tags=tag) == 1


def test_insert_media_stores_stripped_non_empty_parts(user):
    uid, username, _password = user
    insert_media('n3.jpg', None, SPACED, 'h-norm-3', uid)
    parts = _stored(_find('h-norm-3'))
    assert '' not in parts
    assert parts == NORMALIZED + [f'user:{username}']


def test_a_tagless_upload_still_stores_no_tag_of_its_own(user):
    """Normalizing must not invent an empty tag for an untagged upload."""
    uid, username, _password = user
    insert_media('n4.jpg', None, '', 'h-norm-4', uid)
    assert _stored(_find('h-norm-4')) == [f'user:{username}']


def test_update_media_tags_stores_stripped_non_empty_parts(user):
    uid, _username, _password = user
    insert_media('n5.jpg', None, 'initial', 'h-norm-5', uid)
    mid = _find('h-norm-5')
    assert update_media_tags(mid, SPACED, uid)
    assert _stored(mid) == NORMALIZED
    assert get_media_count(include_tags='gamma') == 1


def test_user_tag_membership_is_exact_not_substring(user):
    """`user:alice` must still be appended when `user:alicex` is present.

    The old substring test saw its own tag inside the longer one and
    skipped it, leaving the row without the owner tag the feed filters on.
    """
    uid, username, _password = user
    insert_media('n6.jpg', None, f'user:{username}x', 'h-norm-6', uid)
    assert f'user:{username}' in _stored(_find('h-norm-6'))


def test_published_event_matches_what_is_stored(user):
    """build_media_event's docstring promises it mirrors insert_media."""
    uid, _username, _password = user
    insert_media('n7.jpg', 'n7.png', SPACED, 'h-norm-7', uid)
    event = build_media_event('n7.jpg', 'n7.png', 'h-norm-7', SPACED,
                              uid, '2026-07-27T00:00:00')
    assert event['tags'].split(',') == _stored(_find('h-norm-7'))


def _write_raw(mid, tags):
    """Store a tag string bypassing normalization, as pre-fix rows were."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE media.media SET tags=%s WHERE id=%s", (tags, mid))
        conn.commit()


def test_normalize_tags_backfills_legacy_rows(user):
    """The one-off backfill behind `scripts/tags.py normalize`."""
    uid, _username, _password = user
    insert_media('n8.jpg', None, 'placeholder', 'h-norm-8', uid)
    mid = _find('h-norm-8')
    _write_raw(mid, 'public, beach,video')
    assert get_media_count(include_tags='beach') == 0, "precondition: unfindable"

    changed, changes = normalize_tags()
    assert changed == 1
    assert changes == [(mid, 'public, beach,video', 'public,beach,video')]
    assert get_media_count(include_tags='beach') == 1


def test_normalize_tags_dry_run_writes_nothing(user):
    uid, _username, _password = user
    insert_media('n9.jpg', None, 'placeholder', 'h-norm-9', uid)
    mid = _find('h-norm-9')
    _write_raw(mid, 'public, beach,video')

    changed, changes = normalize_tags(dry_run=True)
    assert changed == 1 and len(changes) == 1
    assert _stored(mid) == ['public', ' beach', 'video'], "dry run must not write"


def test_normalize_tags_leaves_canonical_rows_alone(user):
    """It must report only real edits, not every tagged row."""
    uid, _username, _password = user
    insert_media('n10.jpg', None, 'already,clean', 'h-norm-10', uid)
    assert normalize_tags(dry_run=True) == (0, [])


def test_upload_route_end_to_end(client, logged_in):
    """The path a person actually takes, through the real form."""
    body = client.get('/upload').get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', body).group(1)
    client.post('/upload', data={
        'csrf_token': token,
        'tags_0': 'alpha, beta',
        'files': (_png(), 'spaced.png'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert get_media_count(include_tags='beta') == 1
