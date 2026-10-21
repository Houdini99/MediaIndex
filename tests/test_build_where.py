"""WHERE-clause builder: privacy branches, tag filters, LIKE escaping."""
from app.media.db import _build_where


def _sql(conds):
    return ' AND '.join(conds)


def test_no_filters():
    conds, params = _build_where(None, None, None)
    assert conds == [] and params == []


def test_all_type_is_no_filter():
    conds, _ = _build_where('all', None, None)
    assert conds == []


def test_viewer_hides_others_private():
    conds, params = _build_where(None, None, None, viewer_id=7)
    assert conds == ["((NOT is_private) OR uploaded_by = %s)"]
    assert params == [7]


def test_private_only_restricts_to_own():
    conds, params = _build_where(None, None, None, viewer_id=7, private_only=True)
    assert conds == ["(is_private AND uploaded_by = %s)"]
    assert params == [7]


def test_private_only_without_viewer_is_ignored():
    conds, params = _build_where(None, None, None, viewer_id=None, private_only=True)
    assert conds == [] and params == []


def test_type_filter_uses_the_indexed_column():
    """Types are matched against the stored `ftype`, not by OR-ing ten
    leading-wildcard ILIKEs over `filepath`, which no index can serve.

    `.enc` needs no special handling: ftype is computed from the real
    extension at insert time, so a private file is already 'video'.
    """
    conds, params = _build_where('video', None, None)
    assert conds == ['ftype = ANY(%s)']
    assert params == [['video']]
    assert 'ILIKE' not in _sql(conds)


def test_multiple_types_are_matched_together():
    conds, params = _build_where('image,gif', None, None)
    assert conds == ['ftype = ANY(%s)']
    assert params == [['image', 'gif']]


def test_unknown_type_names_are_dropped():
    """Unrecognised values must not widen the filter into a no-op."""
    conds, params = _build_where('image,bogus', None, None)
    assert params == [['image']]
    assert _build_where('bogus', None, None) == ([], [])


def test_search_is_escaped():
    conds, params = _build_where(None, '100%', None)
    assert conds == ["tags ILIKE %s ESCAPE '\\'"]
    assert params == ['%100\\%%']


def test_search_underscore_escaped():
    _, params = _build_where(None, 'a_b', None)
    assert params == ['%a\\_b%']


def test_search_comma_terms_are_anded():
    conds, params = _build_where(None, 'cat, dog', None)
    assert conds == ["tags ILIKE %s ESCAPE '\\'", "tags ILIKE %s ESCAPE '\\'"]
    assert params == ['%cat%', '%dog%']


def test_search_empty_terms_dropped():
    conds, params = _build_where(None, 'cat, ,', None)
    assert len(conds) == 1
    assert params == ['%cat%']


def test_include_tags_whole_tag_match():
    conds, params = _build_where(None, None, None, include_tags='cat, dog')
    assert len(conds) == 2
    assert all('NOT ILIKE' not in c for c in conds)
    assert params == ['%,cat,%', '%,dog,%']


def test_exclude_tags_whole_tag_match():
    conds, params = _build_where(None, None, 'ai,fake')
    assert len(conds) == 2
    assert all('NOT ILIKE' in c for c in conds)
    assert params == ['%,ai,%', '%,fake,%']


def test_exclude_tag_wildcards_escaped():
    _, params = _build_where(None, None, '50%_off')
    assert params == ['%,50\\%\\_off,%']


def test_combined_filters_order():
    conds, params = _build_where('image', 'meme', 'ai', include_tags='cat', viewer_id=3)
    # privacy first, then type, search, include, exclude
    assert conds[0].startswith('(is_private') or conds[0].startswith('((NOT is_private')
    assert conds[1] == 'ftype = ANY(%s)'
    assert conds[2].startswith('tags ILIKE')
    assert params[0] == 3
    assert params[1] == ['image']
    assert params[2:] == ['%meme%', '%,cat,%', '%,ai,%']
