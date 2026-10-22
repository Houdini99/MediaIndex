"""Bulk tag operations: rename_tag, remove_tag, bulk_edit_tags.

These three had no coverage at all, which was only noticed when
`rename_tag` and `remove_tag` were collapsed onto a shared
`_rewrite_tags`: the suite went green without executing a line of what
had changed. They are reachable from the admin panel *and* from
`scripts/tags.py rename` / `remove`, and they rewrite every
matching row in one shot, so a silent regression here is expensive.

The asymmetry pinned below is deliberate and load-bearing: renaming
de-duplicates (a rename can collide with a tag the row already had),
removing does not (collapsing pre-existing duplicates would be an edit the
caller never asked for). That is exactly the behaviour that would have been
lost by hoisting the dedup into the shared helper.

Note `insert_media` appends a `user:<name>` tag of its own, so assertions
here work on parsed tag sets rather than raw strings wherever that tag
would otherwise be noise.
"""
import pytest

from app.media.db import (
    bulk_edit_tags,
    get_media_by_id,
    insert_media,
    remove_tag,
    rename_tag,
)
from app.media.tagging import tag_list


def _mk(uid, tags, n=0):
    """Insert one media row with `tags`; returns its id."""
    insert_media(f'f{n}.jpg', f'f{n}.png', tags, f'hash{n}', uid)
    from app.media.db import get_media_by_hash
    return get_media_by_hash(f'hash{n}')[0]


def _tags_of(mid):
    return tag_list(get_media_by_id(mid)['tags'])


@pytest.fixture()
def uid(app, user):
    return user[0]


# ── rename_tag ─────────────────────────────────────────────────────────────
def test_rename_replaces_the_tag_everywhere(uid):
    a = _mk(uid, 'holiday,beach', 0)
    b = _mk(uid, 'beach,sunset', 1)

    n, changes = rename_tag('beach', 'seaside')

    assert n == 2
    assert len(changes) == 2
    assert 'seaside' in _tags_of(a) and 'beach' not in _tags_of(a)
    assert 'seaside' in _tags_of(b) and 'beach' not in _tags_of(b)


def test_rename_reports_before_and_after_strings(uid):
    mid = _mk(uid, 'one,two', 0)

    n, changes = rename_tag('one', 'uno')

    assert n == 1
    changed_id, before, after = changes[0]
    assert changed_id == mid
    assert 'one' in before and 'uno' not in before
    assert 'uno' in after and 'one' not in after


def test_rename_deduplicates_on_collision(uid):
    """Renaming onto a tag the row already carries must not double it."""
    mid = _mk(uid, 'cat,kitten', 0)

    n, _ = rename_tag('kitten', 'cat')

    assert n == 1
    tags = _tags_of(mid)
    assert tags.count('cat') == 1


def test_rename_leaves_non_matching_rows_alone(uid):
    other = _mk(uid, 'dog', 0)
    _mk(uid, 'cat', 1)

    n, changes = rename_tag('cat', 'feline')

    assert n == 1
    assert [c[0] for c in changes] == [c[0] for c in changes if c[0] != other]
    assert _tags_of(other) == tag_list(get_media_by_id(other)['tags'])
    assert 'dog' in _tags_of(other)


def test_rename_matches_whole_tags_only(uid):
    """'ai' must not match 'portainer' — the comma-padded ILIKE contract."""
    mid = _mk(uid, 'portainer', 0)

    n, _ = rename_tag('ai', 'artificial')

    assert n == 0
    assert 'portainer' in _tags_of(mid)


def test_rename_dry_run_writes_nothing_but_reports_everything(uid):
    mid = _mk(uid, 'draft,notes', 0)

    n, changes = rename_tag('draft', 'wip', dry_run=True)

    assert n == 1
    assert len(changes) == 1
    assert 'draft' in _tags_of(mid)      # unchanged on disk
    assert 'wip' not in _tags_of(mid)


# ── remove_tag ─────────────────────────────────────────────────────────────
def test_remove_drops_the_tag_everywhere(uid):
    a = _mk(uid, 'keep,drop', 0)
    b = _mk(uid, 'drop', 1)

    n, changes = remove_tag('drop')

    assert n == 2
    assert 'drop' not in _tags_of(a) and 'keep' in _tags_of(a)
    assert 'drop' not in _tags_of(b)


def test_remove_does_not_collapse_pre_existing_duplicates(uid):
    """The counterpart of the rename dedup: removing 'b' from 'a,a,b' must
    leave both copies of 'a' exactly as they were."""
    mid = _mk(uid, 'a,a,b', 0)

    n, _ = remove_tag('b')

    assert n == 1
    assert _tags_of(mid).count('a') == 2


def test_remove_dry_run_writes_nothing(uid):
    mid = _mk(uid, 'temp,real', 0)

    n, changes = remove_tag('temp', dry_run=True)

    assert n == 1 and len(changes) == 1
    assert 'temp' in _tags_of(mid)


def test_remove_matches_whole_tags_only(uid):
    mid = _mk(uid, 'portainer', 0)

    n, _ = remove_tag('ai')

    assert n == 0
    assert 'portainer' in _tags_of(mid)


# ── bulk_edit_tags ─────────────────────────────────────────────────────────
def test_bulk_edit_adds_and_removes(uid):
    a = _mk(uid, 'old', 0)
    b = _mk(uid, 'old,keep', 1)

    updated, failed = bulk_edit_tags([a, b], ['new'], ['old'], uid)

    assert (updated, failed) == (2, 0)
    assert 'new' in _tags_of(a) and 'old' not in _tags_of(a)
    assert 'keep' in _tags_of(b)


def test_bulk_edit_refuses_rows_the_user_does_not_own(uid, app):
    from app.auth.db import insert_user, fetch_user_for_login
    insert_user('mallory', 'x')
    other_uid = fetch_user_for_login('mallory')[0]
    mid = _mk(other_uid, 'private-stuff', 0)

    updated, failed = bulk_edit_tags([mid], ['sneaky'], [], uid)

    assert (updated, failed) == (0, 1)
    assert 'sneaky' not in _tags_of(mid)


def test_bulk_edit_admin_may_edit_any_row(uid, app):
    from app.auth.db import insert_user, fetch_user_for_login
    insert_user('bob', 'x')
    other_uid = fetch_user_for_login('bob')[0]
    mid = _mk(other_uid, 'stuff', 0)

    updated, failed = bulk_edit_tags([mid], ['tagged'], [], uid, is_admin=True)

    assert (updated, failed) == (1, 0)
    assert 'tagged' in _tags_of(mid)


def test_bulk_edit_does_not_duplicate_an_existing_tag(uid):
    mid = _mk(uid, 'already', 0)

    updated, _ = bulk_edit_tags([mid], ['already'], [], uid)

    assert updated == 1
    assert _tags_of(mid).count('already') == 1
