"""Gallery sort options, including seed-stable random pagination."""
import pytest

from app.config import Config
from app.media.db import insert_media


@pytest.fixture()
def many_items(app, user, monkeypatch):
    monkeypatch.setattr(Config, 'PAGE_SIZE', 5)
    uid = user[0]
    for i in range(12):
        insert_media(f'f{i}.jpg', f'f{i}.png', '', f'hash{i}', uid)
    return uid


def _ids(client, **params):
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    data = client.get(f'/api/media?{qs}').get_json()
    return [m['id'] for m in data['media']]


def test_newest_default(client, logged_in, many_items):
    ids = _ids(client)
    assert ids == sorted(ids, reverse=True)


def test_oldest(client, logged_in, many_items):
    ids = _ids(client, sort='oldest')
    assert ids == sorted(ids)


def test_random_pages_are_disjoint_and_stable(client, logged_in, many_items):
    p1 = _ids(client, sort='random', sort_seed=99, page=1)
    p2 = _ids(client, sort='random', sort_seed=99, page=2)
    assert not set(p1) & set(p2)          # no repeats across pages
    assert p1 == _ids(client, sort='random', sort_seed=99, page=1)  # stable
    assert p1 != _ids(client, sort='random', sort_seed=7, page=1)   # seed matters


def test_gallery_random_redirects_with_seed(client, logged_in, many_items):
    resp = client.get('/?sort=random')
    assert resp.status_code == 302
    assert 'sort_seed=' in resp.headers['Location']


@pytest.mark.parametrize('seed', [10**30, 2**63, 5_000_000_000, -1])
def test_out_of_range_seed_is_not_a_server_error(client, logged_in, many_items, seed):
    """The seed comes straight from the query string. Past ~3.4e9 the
    Knuth product overflowed bigint and the API answered 500."""
    resp = client.get(f'/api/media?sort=random&sort_seed={seed}')
    assert resp.status_code == 200
    assert len(resp.get_json()['media']) == 5
    assert client.get(f'/?sort=random&sort_seed={seed}').status_code == 200


def test_seed_folding_keeps_pages_stable(client, logged_in, many_items):
    big = 2**31 + 99
    assert _ids(client, sort='random', sort_seed=big, page=1) == \
        _ids(client, sort='random', sort_seed=big, page=1)
