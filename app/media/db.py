"""All raw SQL for the media domain.

Routes never embed SQL — they call the functions in this module.

Media rows store bare basenames for `filepath` and `thumb` (e.g.
`<sha256>.<ext>` / `<sha256>.png`, plus `.enc` for private files); the
actual directories come from Config.UPLOAD_FOLDER / Config.THUMB_FOLDER.

PostgreSQL-only: schema-qualified names (media.media, users.users), real
booleans, timestamptz values. ILIKE everywhere user text is matched, so
matching is case-insensitive.

Media reads return **mappings, not tuples**, so no route, template or
admin view depends on the physical column order of media.media. With
`SELECT *` tuples indexed positionally across module boundaries (`m[5]`
uploader, `m[6]` date), adding a column in the wrong position would
silently corrupt the JSON API instead of raising.
"""
import logging
import os

from psycopg.rows import dict_row

from ..config import Config
from ..db_utils import db_conn, db_now, like_escape
from ..files import remove_if_exists
from .tagging import tag_list, tag_str, with_user_tag

#: Explicit column list for every media read, so callers see a stable set of
#: names regardless of the table's physical column order.
MEDIA_COLUMNS = ('id, filepath, thumb, tags, filehash, uploaded_by, '
                 'upload_date, is_private, ftype')


def media_path(row_value):
    """Absolute path in UPLOAD_FOLDER for a stored filepath value."""
    return os.path.join(Config.UPLOAD_FOLDER, os.path.basename(row_value))


def thumb_path(row_value):
    """Absolute path in THUMB_FOLDER for a stored thumb value."""
    return os.path.join(Config.THUMB_FOLDER, os.path.basename(row_value))


# ── Username helpers (cross-schema read) ───────────────────────────────────
def get_username_by_id(uid):
    if not uid:
        return None
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT username FROM users.users WHERE id=%s",
                               (uid,)).fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.warning("get_username_by_id failed for uid=%s: %s", uid, e)
        return None


def get_user_id_by_username(username):
    if not username:
        return None
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT id FROM users.users WHERE username=%s",
                               (username,)).fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.warning("get_user_id_by_username failed for %r: %s", username, e)
        return None


def get_usernames_for_media(data):
    ids = {m['uploaded_by'] for m in data if m.get('uploaded_by')}
    if not ids:
        return {}
    try:
        with db_conn() as conn:
            rows = conn.execute("SELECT id, username FROM users.users WHERE id = ANY(%s)",
                                (list(ids),)).fetchall()
        return {uid: name for uid, name in rows}
    except Exception as e:
        logging.warning("get_usernames_for_media failed: %s", e)
        return {}


# ── Shared WHERE-clause builder ────────────────────────────────────────────
def _build_where(ft, search, exclude_tags, include_tags=None, viewer_id=None, private_only=False):
    """Build shared WHERE clause components for media queries.

    `ft` may be a single value ('all'|'image'|'gif'|'video') or a comma-
    separated list of types, in which case the matching extensions are OR-ed.

    `viewer_id`, when set, hides private rows unless they were uploaded by
    the viewer themselves.

    `private_only`, when truthy together with `viewer_id`, restricts the
    result to the viewer's *own* private uploads only.
    """
    conds, params = [], []
    if private_only and viewer_id is not None:
        conds.append("(is_private AND uploaded_by = %s)")
        params.append(viewer_id)
    elif viewer_id is not None:
        conds.append("((NOT is_private) OR uploaded_by = %s)")
        params.append(viewer_id)
    types = []
    if ft and ft != 'all':
        types = [t for t in tag_list(str(ft)) if t != 'all']
    if types:
        # One indexed equality against the stored `ftype`, not a chain of
        # ten leading-wildcard ILIKEs over `filepath` that no index can
        # serve, run twice per gallery page.
        wanted = [t for t in types if t in ('image', 'gif', 'video')]
        if wanted:
            conds.append("ftype = ANY(%s)")
            params.append(wanted)
    if search:
        # Comma-separated terms are AND-ed, each as a substring match —
        # a single term without commas behaves exactly as before.
        for term in tag_list(search):
            conds.append("tags ILIKE %s ESCAPE '\\'")
            params.append(f"%{like_escape(term)}%")
    if include_tags:
        for tag in tag_list(include_tags):
            # Whole-tag match using comma-padded haystack (same trick as exclude).
            conds.append("(tags IS NOT NULL AND (',' || tags || ',') ILIKE %s ESCAPE '\\')")
            params.append(f"%,{like_escape(tag)},%")
    if exclude_tags:
        for tag in tag_list(exclude_tags):
            # Match whole tag only by padding the stored tags with commas on both sides,
            # so "ai" won't match "portainer" or any other tag that merely contains the word.
            conds.append("(tags IS NULL OR (',' || tags || ',') NOT ILIKE %s ESCAPE '\\')")
            params.append(f"%,{like_escape(tag)},%")
    return conds, params


# ── Read queries ───────────────────────────────────────────────────────────
def get_media_count(ft=None, search=None, exclude_tags=None, include_tags=None, viewer_id=None, private_only=False):
    conds, params = _build_where(ft, search, exclude_tags, include_tags, viewer_id, private_only)
    q = "SELECT COUNT(*) FROM media.media"
    if conds:
        q += " WHERE " + " AND ".join(conds)
    with db_conn() as conn:
        return conn.execute(q, params).fetchone()[0]


def get_media(ft=None, search=None, exclude_tags=None, page=1, include_tags=None, viewer_id=None, private_only=False, sort='newest', sort_seed=0):
    conds, params = _build_where(ft, search, exclude_tags, include_tags, viewer_id, private_only)
    if sort == 'oldest':
        order = "id ASC"
    elif sort == 'random':
        # Deterministic per-seed shuffle (Knuth multiplicative hash) so
        # infinite-scroll pages don't repeat items the way RANDOM() would.
        # mod() instead of the % operator (psycopg would treat a bare %
        # as a placeholder escape), ::bigint so the arithmetic can never
        # re-bind to int4. The seed arrives straight from the query string,
        # so it is folded into [0, 2**31): anything much past 3.4e9 made the
        # bigint product overflow and turned the page into a 500.
        seed = int(sort_seed or 0) % 2**31
        order = f"mod(((id + {seed})::bigint * 2654435761), 4294967296)"
    else:
        order = "id DESC"
    with db_conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        q = f"SELECT {MEDIA_COLUMNS} FROM media.media"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += f" ORDER BY {order}"
        q += f" LIMIT {Config.PAGE_SIZE} OFFSET {(page - 1) * Config.PAGE_SIZE}"
        cur.execute(q, params)
        data = cur.fetchall()

    # The uploader name is added as a named key, so every read returns rows
    # of the same shape whichever function built them.
    umap = get_usernames_for_media(data)
    for row in data:
        row['uploader'] = umap.get(row['uploaded_by'], 'Unknown')
    return data


def get_media_ids(ft=None, search=None, exclude_tags=None, include_tags=None, viewer_id=None):
    conds, params = _build_where(ft, search, exclude_tags, include_tags, viewer_id)
    q = "SELECT id FROM media.media"
    if conds:
        q += " WHERE " + " AND ".join(conds)
    with db_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [r[0] for r in rows]


def get_media_by_ids(ids, viewer_id=None):
    if not ids:
        return []
    with db_conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        q = f"SELECT {MEDIA_COLUMNS} FROM media.media WHERE id = ANY(%s)"
        params = [list(ids)]
        if viewer_id is not None:
            q += " AND ((NOT is_private) OR uploaded_by = %s)"
            params.append(viewer_id)
        cur.execute(q, params)
        rows = cur.fetchall()
    by_id = {r['id']: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def get_media_by_id(mid, viewer_id=None):
    with db_conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if viewer_id is not None:
            cur.execute(
                f"SELECT {MEDIA_COLUMNS} FROM media.media WHERE id=%s "
                "AND ((NOT is_private) OR uploaded_by = %s)",
                (mid, viewer_id))
        else:
            cur.execute(f"SELECT {MEDIA_COLUMNS} FROM media.media WHERE id=%s",
                        (mid,))
        return cur.fetchone()


def get_privacy_for_file(fname):
    """Return (uploaded_by, is_private) for the media row whose stored
    filepath or thumb matches `fname`, or None if no such row exists.
    Used by the file-serving routes to block direct access to private media.
    """
    if not fname:
        return None
    with db_conn() as conn:
        return conn.execute(
            "SELECT uploaded_by, is_private FROM media.media "
            "WHERE filepath = %s OR thumb = %s LIMIT 1",
            (fname, fname)).fetchone()


def get_media_privacy_state(mid):
    """Return (filepath, thumb, uploaded_by, is_private) for one row, or
    None — used by the privacy flip in ingest.set_media_privacy."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT filepath, thumb, uploaded_by, is_private "
            "FROM media.media WHERE id=%s",
            (mid,)).fetchone()


def set_media_paths_privacy(mid, filepath, thumb, is_private):
    """Store the renamed file/thumb names and the privacy flag after an
    encrypt/decrypt flip (see ingest.set_media_privacy)."""
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE media.media SET filepath=%s, thumb=%s, is_private=%s WHERE id=%s",
            (filepath, thumb, bool(is_private), mid))
        return cur.rowcount > 0


def replace_thumb(old_thumb, new_thumb):
    """Repoint the row whose thumbnail is `old_thumb` at `new_thumb`.

    Compare-and-set on the old name rather than an update by id: if the row
    changed underneath — deleted, or its privacy flipped, which renames the
    thumbnail — nothing is written and the caller discards the file it just
    rendered. Thumbnail names are unique per row (they embed the file hash),
    so the old name identifies exactly one row. Returns True when repointed.
    """
    with db_conn() as conn:
        cur = conn.execute("UPDATE media.media SET thumb=%s WHERE thumb=%s",
                           (os.path.basename(new_thumb), os.path.basename(old_thumb)))
        return cur.rowcount > 0


def get_image_rows():
    """(id, filepath, thumb, uploaded_by, is_private) for every still-image
    row — the candidates for app.media.thumb_repair."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, filepath, thumb, uploaded_by, is_private "
            "FROM media.media WHERE ftype = 'image' ORDER BY id").fetchall()


def get_all_media_paths():
    """Return (id, filepath, thumb) for every media row — used by the
    admin integrity check."""
    with db_conn() as conn:
        return conn.execute("SELECT id, filepath, thumb FROM media.media").fetchall()


def delete_media_rows(ids):
    """Delete media rows by id without touching the filesystem — used by the
    admin integrity check to drop rows whose files are already gone."""
    if not ids:
        return 0
    with db_conn() as conn:
        return conn.execute("DELETE FROM media.media WHERE id = ANY(%s)",
                            (list(ids),)).rowcount


def hash_exists(fh):
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM media.media WHERE filehash=%s",
                           (fh,)).fetchone()
    return row is not None


def get_media_by_hash(fh):
    """Return (id, filepath, thumb, tags, uploaded_by, is_private) for the
    row with this filehash, or None.

    The upload API uses it to describe *which* item a rejected duplicate
    duplicates, instead of reporting a bare "already indexed".
    """
    if not fh:
        return None
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, filepath, thumb, tags, uploaded_by, is_private "
            "FROM media.media WHERE filehash=%s LIMIT 1",
            (fh,)).fetchone()


def get_tag_statistics(viewer_id=None):
    """[(tag, count)] over visible media, most used first.

    When `viewer_id` is set, tags on private media uploaded by other users
    are excluded so the tag cloud can't leak private content.

    Counted in SQL rather than in Python. Pulling every non-empty `tags`
    string back and splitting/counting/sorting them in a dict spends
    nearly all its time in the Python loop, not the query, and `/tags`,
    `/api/tags` (which drives tag autocomplete) and every admin page
    render call this. Aggregating in SQL is roughly ten times faster.

    A `text[]` column with a GIN index would not help: the storage type
    is not the bottleneck, since the query alone takes about a
    millisecond.
    """
    # LATERAL so btrim() is written once and reused by the filter, the
    # grouping and the ordering.
    sql = """
        SELECT t.tag, count(*) AS n
        FROM media.media,
             LATERAL unnest(string_to_array(tags, ',')) AS raw(part),
             LATERAL (SELECT btrim(raw.part)) AS t(tag)
        WHERE tags IS NOT NULL AND tags <> '' AND t.tag <> ''
        {visibility}
        GROUP BY t.tag
        ORDER BY n DESC, lower(t.tag)
    """
    params = []
    if viewer_id is not None:
        sql = sql.format(visibility="AND ((NOT is_private) OR uploaded_by = %s)")
        params.append(viewer_id)
    else:
        sql = sql.format(visibility="")

    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(tag, count) for tag, count in rows]


# ── Write queries ──────────────────────────────────────────────────────────
def insert_media(fp, th, tags, fh, uid, is_private=False):
    """Insert a media row; returns False when `fh` is already indexed.

    ON CONFLICT DO NOTHING (rather than catching the unique violation)
    so the connection is never left in an aborted transaction — the
    duplicate path is exercised constantly by the Discord bot's dedup.
    """
    with db_conn() as conn:
        udate = db_now()
        uname = get_username_by_id(uid)
        # Normalized at the write boundary, not at the callers. Every read
        # path already parses through tag_list(), and the comma-padded
        # `(',' || tags || ',') ILIKE '%,tag,%'` matching in _build_where is
        # only sound when the stored parts are stripped and non-empty: a
        # form value of "alpha, beta" stored verbatim makes `beta`
        # unfindable forever. Doing it here covers the upload route, the
        # staging promote and the bot's API upload in one place.
        tag_parts = with_user_tag(tags, uname)
        # Falls back to the original value so a tagless upload still stores
        # NULL rather than an empty string.
        tags = tag_str(tag_parts) if tag_parts else tags
        # ftype is stored, not derived at query time — see _build_where.
        # Computed with the same file_type() the rest of the app uses, from
        # the name with any '.enc' suffix stripped.
        from .processing import file_type
        from .crypto import strip_enc
        base = strip_enc(os.path.basename(fp))
        row = conn.execute(
            "INSERT INTO media.media (filepath,thumb,tags,filehash,uploaded_by,upload_date,is_private,ftype) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (filehash) DO NOTHING RETURNING id",
            (os.path.basename(fp), os.path.basename(th) if th else th,
             tags, fh, uid, udate, bool(is_private), file_type(base))).fetchone()
        return row is not None


def delete_media(mid, user_id, is_admin=False):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT filepath,thumb,uploaded_by FROM media.media WHERE id=%s",
            (mid,)).fetchone()
        if not row:
            return False
        if row[2] != user_id and not is_admin:
            return False
        # Row first, files second: the block commits the DELETE as it ends,
        # before the files below are touched. The other way round, a failed
        # DELETE left a row pointing at files that were already gone — a
        # broken gallery item. This way the worst case is an orphaned file,
        # which the admin integrity check finds and cleans up.
        conn.execute("DELETE FROM media.media WHERE id=%s", (mid,))
    # media_path/thumb_path keep only the file name, so the real file in the
    # configured folders is deleted whatever form the stored value has
    # (same as serving).
    remove_media_files(row[0], row[1])
    return True


def remove_media_files(filepath, thumb):
    """Delete a media row's file and thumbnail from disk, if present."""
    if filepath:
        remove_if_exists(media_path(filepath))
    if thumb:
        remove_if_exists(thumb_path(thumb))


# ── Admin: bulk tag operations (also driven by scripts/rename_tag.py and
#    scripts/remove_tag.py, which call straight into these) ─────────────────
def _rewrite_tags(match_tag, transform, dry_run=False):
    """Rewrite the tag list of every row carrying `match_tag`.

    `match_tag=None` walks every tagged row instead — used by
    normalize_tags(), where the selection is "whatever is not already
    canonical" rather than a particular tag.

    `transform` takes the row's parsed tag list and returns the new one.
    Rows it leaves unchanged are skipped, so the count reflects real edits
    only — and a dry run reports exactly what a real run would write,
    because both walk the identical path up to the UPDATE.

    Returns (changed_count, changes) where changes is a list of
    (media_id, old_tags_str, new_tags_str) tuples.
    """
    with db_conn() as conn:
        if match_tag is None:
            rows = conn.execute(
                "SELECT id, tags FROM media.media WHERE tags IS NOT NULL").fetchall()
        else:
            rows = conn.execute(
                "SELECT id, tags FROM media.media "
                "WHERE (',' || tags || ',') ILIKE %s ESCAPE '\\'",
                (f"%,{like_escape(match_tag)},%",)).fetchall()

        changes = []
        for mid, tags_str in rows:
            new_tags_str = tag_str(transform(tag_list(tags_str)))
            if new_tags_str == tags_str:
                continue
            if not dry_run:
                conn.execute("UPDATE media.media SET tags=%s WHERE id=%s",
                             (new_tags_str, mid))
            changes.append((mid, tags_str, new_tags_str))
    return len(changes), changes


def rename_tag(old, new, dry_run=False):
    """Bulk-rename a tag across all media rows.

    Returns (changed_count, changes) where changes is a list of
    (media_id, old_tags_str, new_tags_str) tuples.
    """
    def _rename(tags):
        renamed = [new if t == old else t for t in tags]
        # Deduplicate while preserving order, in case `new` was already on
        # the row. This lives in the transform rather than in
        # _rewrite_tags because only a rename can collide: de-duplicating
        # on the remove path would also collapse pre-existing duplicates
        # the caller never asked to touch.
        seen = set()
        return [t for t in renamed if not (t in seen or seen.add(t))]

    return _rewrite_tags(old, _rename, dry_run)


def remove_tag(tag, dry_run=False):
    """Bulk-remove a tag from all media rows that have it.

    Returns (changed_count, changes) like rename_tag().
    """
    return _rewrite_tags(tag, lambda tags: [t for t in tags if t != tag],
                         dry_run)


def normalize_tags(dry_run=False):
    """Rewrite every row whose stored tag string is not already canonical.

    For rows written by something other than insert_media/
    update_media_tags, which normalize on the way in. A raw value such as
    `"public, beach"` defeats the comma-padded whole-tag matching in
    _build_where, so the padded tag is silently unfindable.

    The transform is the identity: _rewrite_tags already round-trips every
    row through tag_list()/tag_str(), which *is* the normalization, and it
    skips rows that come back unchanged. So this touches only the rows that
    actually need it. Returns (changed_count, changes) like rename_tag().
    """
    return _rewrite_tags(None, lambda tags: tags, dry_run)


def bulk_edit_tags(ids, add_tags, remove_tags, user_id, is_admin=False):
    """Add and/or remove tags on many rows at once.

    `add_tags`/`remove_tags` are lists of tag strings. Rows that don't exist
    or that the user doesn't own (unless admin) count as failed; rows where
    nothing changes still count as updated. Returns (updated, failed).
    """
    remove = set(remove_tags)
    updated = failed = 0
    with db_conn() as conn:
        for mid in ids:
            row = conn.execute("SELECT tags, uploaded_by FROM media.media WHERE id=%s",
                               (mid,)).fetchone()
            if not row or (row[1] != user_id and not is_admin):
                failed += 1
                continue
            tags = tag_list(row[0])
            kept = [t for t in tags if t not in remove]
            merged = kept + [t for t in add_tags if t not in kept]
            new_tags_str = tag_str(merged)
            if new_tags_str != (row[0] or ''):
                conn.execute("UPDATE media.media SET tags=%s WHERE id=%s",
                             (new_tags_str, mid))
            updated += 1
    return updated, failed


def update_media_tags(mid, new_tags, user_id, is_admin=False):
    # The second write funnel into media.tags, and the one the single-media
    # edit form reaches; see insert_media for why this has to be normalized.
    # Idempotent for the callers in admin/dupes.py that already pass
    # tag_str(...).
    new_tags = tag_str(tag_list(new_tags))
    with db_conn() as conn:
        try:
            if is_admin:
                cur = conn.execute("UPDATE media.media SET tags=%s WHERE id=%s",
                                   (new_tags, mid))
            else:
                cur = conn.execute(
                    "UPDATE media.media SET tags=%s WHERE id=%s AND uploaded_by=%s",
                    (new_tags, mid, user_id))
            # Committed here, not when the block ends, so that a failing
            # commit also lands in the handler below.
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            # Roll back so the pooled connection is never handed back
            # mid-aborted-transaction (the pool commits on clean exit).
            conn.rollback()
            return False


# ── User-media reassignment / deletion (admin delete-user cascade) ─────────
def get_media_owned_by(uid):
    """Return (id, filepath, thumb, is_private) for every row uploaded by `uid`."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, filepath, thumb, is_private FROM media.media "
            "WHERE uploaded_by=%s", (uid,)).fetchall()


def reassign_media_owner(old_uid, new_uid):
    """Reassign all of old_uid's media rows to new_uid. Returns rows changed.

    Only valid when old_uid has no private (encrypted) media — encrypted
    files can only be decrypted with the original owner's key.
    """
    with db_conn() as conn:
        return conn.execute(
            "UPDATE media.media SET uploaded_by=%s WHERE uploaded_by=%s",
            (new_uid, old_uid)).rowcount


def delete_media_for_user(uid):
    """Delete all media rows uploaded by `uid`, removing files and thumbs
    from disk. Returns the number of rows deleted.

    Rows go first, for the same reason as in delete_media()."""
    rows = get_media_owned_by(uid)
    deleted = delete_media_rows([r[0] for r in rows])
    for _, fp, th, _ in rows:
        remove_media_files(fp, th)
    return deleted
