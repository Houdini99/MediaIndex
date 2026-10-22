"""Tag-string helpers.

Tags are stored as one comma-separated string per media row (`media.tags`),
so every consumer that wants to reason about individual tags has to split
it, strip the parts, and drop the empties that a trailing comma leaves
behind. That three-step dance was written inline in seven places and as two
private helpers with different names, in three modules.

The parts are always stripped and never empty, which is what makes the
comma-padded `(',' || tags || ',') ILIKE '%,tag,%'` whole-tag matching in
`app.media.db._build_where` sound: a stored `"a, b"` would not match
`%,b,%`. Writing the parse once keeps that invariant in one place instead
of depending on seven copies agreeing.

Pure string handling — no Flask, no SQL, mirroring processing.py.
"""


def tag_list(tags):
    """Stored tag string → list of stripped, non-empty tags.

    Accepts None (an unset `tags` column) and returns an empty list, so
    callers do not each need their own `or ''` guard.
    """
    return [t.strip() for t in (tags or '').split(',') if t.strip()]


def tag_str(tags):
    """List of tags → the stored comma-separated form. Inverse of tag_list."""
    return ','.join(tags)


def with_user_tag(tags, username):
    """tag_list(tags) plus `user:<username>`, the tag that names an item's
    owner, unless it is already there. Exact membership rather than a
    substring test: `user:bob` is not present just because `user:bobby` is.
    """
    parts = tag_list(tags)
    if username and f"user:{username}" not in parts:
        parts.append(f"user:{username}")
    return parts
