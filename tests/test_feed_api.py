"""Contract for /api/feed and the delete route the feed calls.

The feed is entirely client-driven — one JSON endpoint feeding an infinite
scroller — so nothing server-side exercised it before. These cover the parts
the JS genuinely depends on and cannot check for itself:

  * a seed produces one stable order, because pagination slices that order
    and a reshuffle between pages would skip or repeat items;
  * `has_more` is exact at a page boundary, because it is the only thing
    telling the client when a cycle has ended;
  * `can_delete` matches what delete_media will actually permit, because the
    feed hides the button on the strength of it;
  * `/delete/<id>` reports refusal *as* a refusal to a scripted caller. It
    used to answer with a redirect, which fetch follows to a 200 — so the
    feed removed slides the server had refused to delete.

Rows go in through insert_media rather than the upload route: these are
query-shape assertions, and paying for PIL and ffmpeg per row would put the
page-boundary cases out of reach.
"""
import pytest
from werkzeug.security import generate_password_hash

from app.config import Config
from app.auth.db import insert_user, fetch_user_for_login, set_user_admin
from app.media.db import insert_media


def _clear_feed_cache():
    """The shuffled-id cache is a module global that outlives a test."""
    from app.media import routes
    with routes._feed_cache_lock:
        routes._feed_cache.clear()


@pytest.fixture(autouse=True)
def _fresh_cache():
    _clear_feed_cache()
    yield
    _clear_feed_cache()


def _seed_media(uid, count, ext='png', tags='sample', private=False, start=0):
    """Insert `count` rows owned by `uid`; returns their filehashes' order."""
    for i in range(start, start + count):
        insert_media(f'file{i}.{ext}', f'thumb{i}.{ext}', tags,
                     f'hash-{ext}-{i}', uid, is_private=private)


def _page(client, **params):
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    resp = client.get(f'/api/feed?{qs}')
    assert resp.status_code == 200
    return resp.get_json()


def _all_ids(client, seed=999, **params):
    ids, page = [], 1
    while True:
        payload = _page(client, seed=seed, page=page, **params)
        ids += [m['id'] for m in payload['media']]
        if not payload.get('has_more'):
            break
        page += 1
        assert page < 50, 'feed did not terminate'
    return ids


# ── Ordering ───────────────────────────────────────────────────────────────
def test_seed_produces_a_stable_order(client, logged_in):
    """Pagination slices one shuffled list. If the same seed reshuffled
    between requests, page 2 would be cut from a different order than page 1
    and items would silently vanish from the cycle."""
    _seed_media(logged_in, 12)
    first = [m['id'] for m in _page(client, seed=42, page=1)['media']]
    _clear_feed_cache()          # force the shuffle to be recomputed
    again = [m['id'] for m in _page(client, seed=42, page=1)['media']]
    assert first == again
    assert len(first) == 12


def test_different_seeds_reorder_the_same_rows(client, logged_in):
    _seed_media(logged_in, 25)
    a = _all_ids(client, seed=1)
    b = _all_ids(client, seed=2)
    assert sorted(a) == sorted(b)      # same rows
    assert a != b                      # different order


# ── Pagination ─────────────────────────────────────────────────────────────
def test_has_more_is_false_on_an_exact_page_boundary(client, logged_in, monkeypatch):
    """The off-by-one that matters: with exactly PAGE_SIZE rows there is no
    second page, and claiming otherwise makes the client fetch an empty one."""
    monkeypatch.setattr(Config, 'PAGE_SIZE', 5)
    _seed_media(logged_in, 5)
    payload = _page(client, seed=7, page=1)
    assert len(payload['media']) == 5
    assert payload['has_more'] is False
    assert payload['total'] == 5


def test_pages_partition_the_library_without_gaps_or_repeats(client, logged_in, monkeypatch):
    monkeypatch.setattr(Config, 'PAGE_SIZE', 4)
    _seed_media(logged_in, 10)
    ids = _all_ids(client, seed=3)
    assert len(ids) == 10
    assert len(set(ids)) == 10


def test_page_beyond_the_end_is_empty_not_an_error(client, logged_in, monkeypatch):
    monkeypatch.setattr(Config, 'PAGE_SIZE', 4)
    _seed_media(logged_in, 4)
    payload = _page(client, seed=3, page=9)
    assert payload['media'] == []
    assert payload['has_more'] is False


@pytest.mark.parametrize('page', ['0', '-3', 'abc', ''])
def test_junk_page_values_fall_back_to_the_first_page(client, logged_in, page):
    _seed_media(logged_in, 3)
    payload = _page(client, seed=5, page=page)
    assert len(payload['media']) == 3


def test_junk_seed_does_not_error(client, logged_in):
    _seed_media(logged_in, 3)
    assert len(_page(client, seed='not-a-number', page=1)['media']) == 3


# ── Filtering ──────────────────────────────────────────────────────────────
def test_types_takes_precedence_over_legacy_type(client, logged_in):
    _seed_media(logged_in, 3, ext='png', start=0)
    _seed_media(logged_in, 2, ext='mp4', start=10)
    payload = _page(client, seed=1, page=1, types='video', type='image')
    assert {m['type'] for m in payload['media']} == {'video'}
    assert payload['total'] == 2


def test_unknown_type_values_are_ignored_not_matched(client, logged_in):
    """`wanted` filters the list down to known ftypes; a junk entry must not
    become a WHERE clause that matches nothing (or everything)."""
    _seed_media(logged_in, 3)
    payload = _page(client, seed=1, page=1, types='bogus,image')
    assert payload['total'] == 3


def test_hide_ai_excludes_the_configured_tags(client, logged_in):
    """The toggle the feed UI can finally reach."""
    _seed_media(logged_in, 2, tags='landscape', start=0)
    _seed_media(logged_in, 3, tags='ai', start=10)
    assert _page(client, seed=1, page=1)['total'] == 5
    assert _page(client, seed=1, page=1, hide_ai=1)['total'] == 2


def test_hide_ai_matches_whole_tags_only(client, logged_in):
    """'ai' must not knock out 'portrait' or any tag merely containing it."""
    _seed_media(logged_in, 1, tags='chair', start=0)
    _seed_media(logged_in, 1, tags='airbrush', start=10)
    _seed_media(logged_in, 1, tags='ai', start=20)
    assert _page(client, seed=1, page=1, hide_ai=1)['total'] == 2


def test_include_and_exclude_tags_combine(client, logged_in):
    _seed_media(logged_in, 1, tags='cat,indoor', start=0)
    _seed_media(logged_in, 1, tags='cat,outdoor', start=10)
    payload = _page(client, seed=1, page=1, include_tags='cat', exclude_tags='outdoor')
    assert payload['total'] == 1


# ── can_delete ─────────────────────────────────────────────────────────────
def test_can_delete_is_true_for_your_own_media(client, logged_in):
    _seed_media(logged_in, 2)
    assert all(m['can_delete'] for m in _page(client, seed=1, page=1)['media'])


def test_can_delete_is_false_for_someone_elses_media(client, logged_in):
    """The feed hides the delete button on this flag. If it lied, the button
    would appear and the server would refuse it."""
    insert_user('bob', generate_password_hash('password123'))
    bob_id = fetch_user_for_login('bob')[0]
    set_user_admin(bob_id, False)
    _seed_media(bob_id, 2)

    payload = _page(client, seed=1, page=1)
    assert payload['total'] == 2
    assert all(m['can_delete'] is False for m in payload['media'])


def test_admin_can_delete_anything(client, app, user):
    insert_user('carol', generate_password_hash('password123'))
    carol_id = fetch_user_for_login('carol')[0]
    _seed_media(carol_id, 1)

    admin_uid, admin_name, admin_pw = user
    set_user_admin(admin_uid, True)
    client.post('/login', data={'username': admin_name, 'password': admin_pw})

    payload = _page(client, seed=1, page=1)
    assert all(m['can_delete'] for m in payload['media'])


# ── The delete route the feed calls ────────────────────────────────────────
def _media_ids(client):
    return [m['id'] for m in _page(client, seed=1, page=1)['media']]


def test_scripted_delete_of_your_own_media_returns_json_ok(client, logged_in):
    _seed_media(logged_in, 1)
    mid = _media_ids(client)[0]
    resp = client.post(f'/delete/{mid}',
                       headers={'X-Requested-With': 'XMLHttpRequest'})
    assert resp.status_code == 200
    assert resp.get_json() == {'deleted': True}


def test_scripted_delete_of_someone_elses_media_is_refused(client, logged_in):
    """The regression that made the feed lie: this used to answer 302 to the
    gallery, which fetch follows to a 200."""
    insert_user('bob', generate_password_hash('password123'))
    bob_id = fetch_user_for_login('bob')[0]
    _seed_media(bob_id, 1)
    mid = _media_ids(client)[0]

    resp = client.post(f'/delete/{mid}',
                       headers={'X-Requested-With': 'XMLHttpRequest'})
    assert resp.status_code == 403
    assert resp.get_json() == {'deleted': False}
    # and the row is genuinely still there
    _clear_feed_cache()
    assert mid in _media_ids(client)


def test_form_post_delete_still_redirects(client, logged_in):
    """Non-scripted callers keep the flash-and-redirect behaviour; the JSON
    branch is opt-in via the header MI.post sends."""
    _seed_media(logged_in, 1)
    mid = _media_ids(client)[0]
    resp = client.post(f'/delete/{mid}')
    assert resp.status_code == 302


# ── Shuffled-id cache ──────────────────────────────────────────────────────
def test_cache_is_keyed_by_viewer(client, logged_in):
    """Two users share a seed and filters; the cache key must still separate
    them, or one viewer's visible set could be served to the other."""
    from app.media import routes
    _seed_media(logged_in, 2, private=True)
    _all_ids(client, seed=77)

    insert_user('bob', generate_password_hash('password123'))
    client.post('/logout')
    client.post('/login', data={'username': 'bob', 'password': 'password123'})
    assert _all_ids(client, seed=77) == []          # bob sees no private rows
    assert len(routes._feed_cache) == 2             # two distinct entries


def test_cache_entries_are_evicted_at_the_cap(client, logged_in):
    from app.media import routes
    _seed_media(logged_in, 1)
    for seed in range(routes._FEED_CACHE_MAX + 10):
        _page(client, seed=seed, page=1)
    assert len(routes._feed_cache) <= routes._FEED_CACHE_MAX


def test_expired_cache_entry_is_recomputed(client, logged_in, monkeypatch):
    """New uploads have to become reachable eventually — the TTL is the only
    thing that makes that happen."""
    from app.media import routes
    _seed_media(logged_in, 2)
    assert _page(client, seed=5, page=1)['total'] == 2

    _seed_media(logged_in, 3, start=50)
    assert _page(client, seed=5, page=1)['total'] == 2      # still cached

    monkeypatch.setattr(routes, '_FEED_CACHE_TTL', -1)      # everything is stale
    assert _page(client, seed=5, page=1)['total'] == 5


# ── Auth ───────────────────────────────────────────────────────────────────
def test_feed_api_requires_login(client):
    resp = client.get('/api/feed?seed=1&page=1')
    assert resp.status_code in (302, 401)
