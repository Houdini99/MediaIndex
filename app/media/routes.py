"""Media routes — gallery, upload, feed, tags, and JSON APIs.

All persistence calls go through app.media.db; image/video processing
calls go through app.media.processing.
"""
import hmac
import mimetypes
import os
import random
import tempfile
import threading
import time
from flask import (
    render_template, request, redirect, url_for, flash,
    send_from_directory, jsonify, g, abort, Response
)
from . import media_bp
from .db import (
    get_media,
    get_media_count,
    get_media_ids,
    get_media_by_ids,
    get_media_by_id,
    get_media_by_hash,
    get_privacy_for_file,
    get_usernames_for_media,
    get_username_by_id,
    get_user_id_by_username,
    get_tag_statistics,
    delete_media,
    update_media_tags,
    bulk_edit_tags,
)
from .processing import allowed_file, file_type, save_stream_hashed
from .tagging import tag_list
from .crypto import ENC_SUFFIX, iter_decrypted, plaintext_size, strip_enc
from .ingest import ingest_file, set_media_privacy
from ..auth.db import (
    fetch_user_for_dav,
    get_user_enc_key,
    hash_dav_password,
    claim_discord_link_code,
    resolve_discord_user,
    resolve_telegram_user,
)
from ..auth.routes import login_required
from ..config import Config
from ..extensions import csrf, limiter
from .. import site_logins
from ..security_log import failed_login


def _ai_exclude(exclude_tags):
    """Append the configured AI tags to an exclude_tags string."""
    ai_tags = ','.join(Config.AI_TAGS)
    return (exclude_tags + ',' + ai_tags).strip(',') if exclude_tags else ai_tags


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (ValueError, TypeError):
        return default


# ── Pages ─────────────────────────────────────────────────────────────────
@media_bp.route('/')
@login_required
def index():
    hide_ai = request.args.get('hide_ai', '')
    private_only = request.args.get('private_only', '') == '1'
    sort = request.args.get('sort', 'newest')
    # Random sort needs a stable seed in the URL so the infinite-scroll API
    # pages continue the same ordering instead of reshuffling per request.
    if sort == 'random' and not request.args.get('sort_seed'):
        args = request.args.to_dict()
        args['sort_seed'] = str(random.randint(1, 2**31))
        return redirect(url_for('media.index', **args))
    sort_seed = _int_arg('sort_seed', 0)
    exclude_tags = request.args.get('exclude_tags', '')
    if hide_ai == '1':
        exclude_tags = _ai_exclude(exclude_tags)
    media = get_media(
        request.args.get('type', 'all'),
        request.args.get('search', ''),
        exclude_tags,
        viewer_id=g.user['id'],
        private_only=private_only,
        sort=sort,
        sort_seed=sort_seed,
    )
    return render_template(
        'index.html',
        media=media,
        search=request.args.get('search', ''),
        exclude_tags=request.args.get('exclude_tags', ''),
        filter_type=request.args.get('type', 'all'),
        hide_ai=hide_ai,
        private_only=private_only,
        sort=sort,
    )


def _owner_uid(uid, username, uploaded_for):
    """Resolve the index user an upload should be attributed to.

    Delegated attribution: trusted accounts (the Discord bot) may name the
    index user who owns the upload; unknown names keep the uploader as
    owner so ingestion never drops media over a mapping typo.
    """
    uploaded_for = (uploaded_for or '').strip()
    if uploaded_for and username in Config.DELEGATED_UPLOADERS:
        return get_user_id_by_username(uploaded_for) or uid
    return uid


def _store_upload(f, tags, uid, is_private):
    """Run one uploaded file through the ingest pipeline.

    Returns (status, filehash) with status 'ok' | 'duplicate' | 'error';
    'error' means the extension is not allowed and nothing was written.
    Files are stored as <sha256>.<ext>, so only the extension has to be
    filesystem-safe — non-ASCII original names are fine.
    """
    ext = os.path.splitext(os.path.basename(f.filename or ''))[1].lstrip('.').lower()
    if not ext or not allowed_file(f"f.{ext}"):
        return 'error', None
    with tempfile.NamedTemporaryFile(dir=Config.UPLOAD_FOLDER, delete=False,
                                     suffix=f'.{ext}') as tf:
        tmp = tf.name
    try:
        fh = save_stream_hashed(f.stream, tmp)
    except BaseException:
        # A client that disconnects mid-upload would otherwise leave the
        # partial temp file behind in UPLOAD_FOLDER for the orphan cleanup.
        os.remove(tmp)
        raise
    return ingest_file(tmp, ext, tags, uid, is_private, filehash=fh)


@media_bp.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        files = request.files.getlist('files')
        results = {'uploaded': [], 'duplicates': [], 'errors': []}
        uid = g.user['id'] if g.user else None
        if not uid:
            flash("❌ Error: not logged in.", "danger")
            return redirect(url_for('media.upload'))
        uid = _owner_uid(uid, g.user['username'],
                         request.form.get('uploaded_for'))
        for i, f in enumerate(files):
            fn = f.filename or '(unnamed)'
            tags = request.form.get(f'tags_{i}', '')
            is_private = request.form.get(f'private_{i}') in ('1', 'on', 'true')
            status, _ = _store_upload(f, tags, uid, is_private)
            if status == 'ok':
                results['uploaded'].append(fn)
            elif status == 'duplicate':
                results['duplicates'].append(fn)
            else:
                results['errors'].append(fn)
        for k, v in results.items():
            if v:
                if k == 'uploaded':
                    flash(f"✅Successful: {', '.join(v)}", "success")
                elif k == 'duplicates':
                    flash(f"⚠️ Duplicates: {', '.join(v)}", "warning")
                else:
                    flash(f"❌ Error: {', '.join(v)}", "danger")
        return redirect(url_for('media.upload'))
    return render_template('upload.html')


@media_bp.route('/feed')
@login_required
def feed():
    # The panel names the tags the toggle hides rather than making the reader
    # guess; same list the API filters on, so it cannot drift.
    return render_template('feed.html', ai_tags=Config.AI_TAGS)


@media_bp.route('/tags')
@login_required
def tags():
    """Show tag statistics page"""
    tag_stats = get_tag_statistics(viewer_id=g.user['id'])
    total_tags = len(tag_stats)
    total_usages = sum(count for _, count in tag_stats)
    return render_template(
        'tags.html',
        tag_stats=tag_stats,
        total_tags=total_tags,
        total_usages=total_usages
    )


# ── File serving ──────────────────────────────────────────────────────────
def _authorize_file(fname):
    """Authorize access to a stored media/thumb file, failing CLOSED.

    Access is granted only when `fname` belongs to a known media row that is
    public or owned by the current user. A name with no matching row is
    refused — an orphan, or (crucially) a transient plaintext file that exists
    on disk mid-encrypt/decrypt before its row points at the '.enc' name. This
    is why the check must not fail open: those unindexed plaintext artifacts
    must never be served just because no row currently claims their name.
    """
    row = get_privacy_for_file(fname)
    if not row:
        abort(404)
    owner_uid, is_private = row[0], row[1]
    if is_private and owner_uid != g.user['id']:
        abort(404)


def _serve_encrypted(folder, fname):
    """Stream a `.enc` file decrypted on the fly, honoring HTTP Range requests.

    Owner is taken from the media row (not from the requesting session) so the
    correct key is used even if a future change ever allowed shared access.
    """
    path = os.path.join(folder, fname)
    if not os.path.isfile(path):
        abort(404)
    row = get_privacy_for_file(fname)
    if not row:
        abort(404)
    owner_uid = row[0]
    key = get_user_enc_key(owner_uid)
    if not key:
        abort(404)

    total = plaintext_size(path)
    start, end_inclusive = 0, total - 1
    status = 200
    headers = {'Accept-Ranges': 'bytes'}

    range_header = request.headers.get('Range', '')
    if range_header.startswith('bytes=') and total > 0:
        try:
            spec = range_header[6:].split(',', 1)[0].strip()
            s_part, e_part = spec.split('-', 1)
            start = int(s_part) if s_part else 0
            end_inclusive = int(e_part) if e_part else total - 1
            if start < 0 or end_inclusive >= total or start > end_inclusive:
                raise ValueError
        except ValueError:
            return Response(
                status=416,
                headers={'Content-Range': f'bytes */{total}', 'Accept-Ranges': 'bytes'},
            )
        status = 206
        headers['Content-Range'] = f'bytes {start}-{end_inclusive}/{total}'

    headers['Content-Length'] = str(end_inclusive - start + 1)
    mime, _ = mimetypes.guess_type(strip_enc(fname))
    headers['Content-Type'] = mime or 'application/octet-stream'

    return Response(
        iter_decrypted(path, key, start, end_inclusive + 1),
        status=status,
        headers=headers,
    )


@media_bp.route('/thumbs/<fname>')
@login_required
def serve_thumb(fname):
    _authorize_file(fname)
    if fname.endswith(ENC_SUFFIX):
        return _serve_encrypted(Config.THUMB_FOLDER, fname)
    return send_from_directory(Config.THUMB_FOLDER, fname)


@media_bp.route('/media/<fname>')
@login_required
def serve_media(fname):
    _authorize_file(fname)
    if fname.endswith(ENC_SUFFIX):
        return _serve_encrypted(Config.UPLOAD_FOLDER, fname)
    return send_from_directory(Config.UPLOAD_FOLDER, fname)


# ── Mutations ─────────────────────────────────────────────────────────────
def _wants_json():
    """True when the caller is fetch()/XHR rather than a browser navigation.

    The delete route answers both: a plain form POST still wants the flash +
    redirect, but `fetch` follows that redirect and lands on a 200 gallery
    page, so `response.ok` says "deleted" even when the row was refused.
    Scripted callers get the real outcome as JSON instead.
    """
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.accept_mimetypes.best == 'application/json'
    )


@media_bp.route('/delete/<int:media_id>', methods=['POST'])
@login_required
def delete(media_id):
    ok = delete_media(media_id, g.user['id'], is_admin=g.user['is_admin'])
    if _wants_json():
        # No flash: nothing renders it before the next real page load, so it
        # would surface later attached to an unrelated view.
        return jsonify({'deleted': ok}), (200 if ok else 403)
    if ok:
        flash("✅ File deleted.", "success")
    else:
        flash("❌ Error deleting file.", "danger")
    return redirect(url_for('media.index'))


@media_bp.route('/delete-bulk', methods=['POST'])
@login_required
def delete_bulk():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'ids must be a non-empty list'}), 400
    try:
        ids = [int(i) for i in ids[:500]]
    except (ValueError, TypeError):
        return jsonify({'error': 'ids must be integers'}), 400
    deleted = sum(
        1 for mid in ids
        if delete_media(mid, g.user['id'], is_admin=g.user['is_admin'])
    )
    failed = len(ids) - deleted
    if deleted:
        flash(f"✅ {deleted} file{'s' if deleted != 1 else ''} deleted.", "success")
    if failed:
        flash(f"❌ {failed} file{'s' if failed != 1 else ''} could not be deleted.", "danger")
    return jsonify({'deleted': deleted, 'failed': failed})


@media_bp.route('/privacy-bulk', methods=['POST'])
@login_required
def privacy_bulk():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'ids must be a non-empty list'}), 400
    try:
        ids = [int(i) for i in ids[:500]]
    except (ValueError, TypeError):
        return jsonify({'error': 'ids must be integers'}), 400
    private = data.get('private')
    if not isinstance(private, bool):
        return jsonify({'error': 'private must be a boolean'}), 400
    updated = sum(
        1 for mid in ids
        if set_media_privacy(mid, private, g.user['id'], is_admin=g.user['is_admin'])
    )
    failed = len(ids) - updated
    word = 'private' if private else 'public'
    if updated:
        flash(f"✅ {updated} file{'s' if updated != 1 else ''} made {word}.", "success")
    if failed:
        flash(f"❌ {failed} file{'s' if failed != 1 else ''} could not be made {word}.", "danger")
    return jsonify({'updated': updated, 'failed': failed})


@media_bp.route('/edit-bulk', methods=['POST'])
@login_required
def edit_bulk():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'ids must be a non-empty list'}), 400
    try:
        ids = [int(i) for i in ids[:500]]
    except (ValueError, TypeError):
        return jsonify({'error': 'ids must be integers'}), 400

    def _tag_field(key):
        """Parsed tags from a JSON string field, or None if it isn't one."""
        raw = data.get(key, '')
        if not isinstance(raw, str):
            return None
        return tag_list(raw)

    add, remove = _tag_field('add'), _tag_field('remove')
    if add is None or remove is None:
        return jsonify({'error': 'add/remove must be comma-separated strings'}), 400
    if not add and not remove:
        return jsonify({'error': 'nothing to change'}), 400
    updated, failed = bulk_edit_tags(ids, add, remove, g.user['id'], is_admin=g.user['is_admin'])
    if updated:
        flash(f"✅ Tags updated on {updated} file{'s' if updated != 1 else ''}.", "success")
    if failed:
        flash(f"❌ {failed} file{'s' if failed != 1 else ''} could not be updated.", "danger")
    return jsonify({'updated': updated, 'failed': failed})


@media_bp.route('/edit/<int:media_id>', methods=['POST'])
@login_required
def edit_tags(media_id):
    new_tags = request.form.get('tags', '').strip()
    if update_media_tags(media_id, new_tags, g.user['id'], is_admin=g.user['is_admin']):
        flash("✅ Tags successfully updated.", "success")
    else:
        flash("❌ Error updating tags.", "danger")
    return redirect(url_for('media.index'))


# ── JSON APIs ─────────────────────────────────────────────────────────────
@media_bp.route('/mediadata/<int:media_id>')
@login_required
def media_meta(media_id):
    m = get_media_by_id(media_id, viewer_id=g.user['id'])
    if m:
        fp_base = os.path.basename(m['filepath'])
        orig_name = strip_enc(fp_base)
        return jsonify({
            'id': m['id'],
            'filepath': url_for('media.serve_media', fname=fp_base),
            'download_name': orig_name,
            'thumb': url_for('media.serve_thumb',
                             fname=os.path.basename(m['thumb'])),
            'tags': m['tags'],
            'type': file_type(orig_name),
            'uploaded_by': get_username_by_id(m['uploaded_by']) or 'Unknown',
            # timestamptz comes back as datetime; keep the ISO string the
            # API always served (jsonify would render RFC-822 instead).
            'upload_date': m['upload_date'].isoformat() if m['upload_date'] else None,
            'is_private': bool(m['is_private']),
        })
    return jsonify({'error': 'Not found'}), 404


@media_bp.route('/api/media')
@login_required
def api_media():
    ft = request.args.get('type', 'all')
    search = request.args.get('search', '')
    hide_ai = request.args.get('hide_ai', '')
    private_only = request.args.get('private_only', '') == '1'
    sort = request.args.get('sort', 'newest')
    sort_seed = _int_arg('sort_seed', 0)
    exclude_tags = request.args.get('exclude_tags', '')
    if hide_ai == '1':
        exclude_tags = _ai_exclude(exclude_tags)
    page = max(1, min(_int_arg('page', 1), 9999))
    media = get_media(ft, search, exclude_tags, page, viewer_id=g.user['id'], private_only=private_only, sort=sort, sort_seed=sort_seed)
    total = get_media_count(ft, search, exclude_tags, viewer_id=g.user['id'], private_only=private_only)
    has_more = (page * Config.PAGE_SIZE) < total
    result = [
        {
            'id': m['id'],
            'thumb': os.path.basename(m['thumb']),
            'tags': m['tags'] or '',
            'type': file_type(strip_enc(m['filepath'])),
            'is_private': bool(m['is_private']),
        }
        for m in media
    ]
    return jsonify({'media': result, 'has_more': has_more})


# Shuffled-ID cache for the feed: without it every "load more" re-fetched all
# matching IDs and reshuffled the full list just to slice one page. Keyed by
# (seed + filters + viewer); entries expire so new uploads appear eventually.
_FEED_CACHE_TTL = 300
_FEED_CACHE_MAX = 64
_feed_cache = {}
_feed_cache_lock = threading.Lock()


def _shuffled_feed_ids(seed, ft, search, exclude_tags, include_tags, viewer_id):
    key = (seed, ft, search, exclude_tags, include_tags, viewer_id)
    now = time.monotonic()
    with _feed_cache_lock:
        hit = _feed_cache.get(key)
        if hit and now - hit[0] < _FEED_CACHE_TTL:
            return hit[1]
    ids = get_media_ids(ft, search, exclude_tags, include_tags=include_tags, viewer_id=viewer_id)
    random.Random(seed).shuffle(ids)
    with _feed_cache_lock:
        if len(_feed_cache) >= _FEED_CACHE_MAX:
            del _feed_cache[min(_feed_cache, key=lambda k: _feed_cache[k][0])]
        _feed_cache[key] = (now, ids)
    return ids


@media_bp.route('/api/feed')
@login_required
def api_feed():
    # `types` (comma-separated) takes precedence over the legacy single `type`.
    ft = request.args.get('types') or request.args.get('type', 'all')
    search = request.args.get('search', '')
    include_tags = request.args.get('include_tags', '')
    hide_ai = request.args.get('hide_ai', '')
    exclude_tags = request.args.get('exclude_tags', '')
    if hide_ai == '1':
        exclude_tags = _ai_exclude(exclude_tags)
    try:
        seed = int(request.args.get('seed', '0'))
    except (ValueError, TypeError):
        seed = 0
    try:
        page = max(1, min(int(request.args.get('page', 1)), 99999))
    except (ValueError, TypeError):
        page = 1

    ids = _shuffled_feed_ids(seed, ft, search, exclude_tags, include_tags, g.user['id'])

    start = (page - 1) * Config.PAGE_SIZE
    end = start + Config.PAGE_SIZE
    page_ids = ids[start:end]
    has_more = end < len(ids)

    rows = get_media_by_ids(page_ids, viewer_id=g.user['id'])
    umap = get_usernames_for_media(rows)
    # Mirrors delete_media's ownership rule (db.py) so the feed can hide a
    # delete button the server would only refuse. `uploaded_by` in the payload
    # is a display name, which is why the check reads the row's id instead.
    is_admin = bool(g.user['is_admin'])
    result = [
        {
            'id': m['id'],
            'filepath': os.path.basename(m['filepath']),
            'download_name': strip_enc(os.path.basename(m['filepath'])),
            'thumb': os.path.basename(m['thumb']),
            'tags': m['tags'] or '',
            'type': file_type(strip_enc(m['filepath'])),
            'uploaded_by': umap.get(m['uploaded_by'], 'Unknown'),
            'is_private': bool(m['is_private']),
            'can_delete': is_admin or m['uploaded_by'] == g.user['id'],
        }
        for m in rows
    ]
    return jsonify({'media': result, 'has_more': has_more, 'total': len(ids)})


@media_bp.route('/api/tags')
@login_required
def api_tags():
    tag_stats = get_tag_statistics(viewer_id=g.user['id'])
    return jsonify(sorted(tag for tag, _ in tag_stats))


# ── Upload API (Discord bot, scripts) ─────────────────────────────────────
# Authenticated with HTTP Basic against an index account's *app password*
# (the one generated for WebDAV on /queue) — never the login password, and
# never the browser session. Because it carries no ambient cookie
# authority, exempting it from CSRF is safe; and because it answers in
# JSON, a client never has to scrape CSRF tokens out of the upload form
# or results out of flash markup.
def _api_user():
    """Authenticate an API request from its Basic credentials; returns
    {'id', 'username'} or None.

    A bad credential feeds the same IP-ban counter as a failed web login
    (mirroring app.dav.dc), so brute-forcing the API is throttled exactly
    like brute-forcing /login or /dav.
    """
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return None
    row = fetch_user_for_dav(auth.username)
    if not row or not row[1] or not hmac.compare_digest(
            hash_dav_password(auth.password), row[1]):
        failed_login(auth.username, 'api', known=row is not None)
        return None
    return {'id': row[0], 'username': auth.username}


def _duplicate_of(fh):
    """Describe the already-indexed item a rejected duplicate collided
    with, or None if it can't be found.

    Private items report only that they are private: the caller is a
    Discord channel, and it must not learn another user's private tags or
    ownership just by re-posting a file that happens to hash the same.
    """
    row = get_media_by_hash(fh)
    if not row:
        return None
    mid, filepath, _thumb, tags, owner, is_private = row
    if is_private:
        return {'id': mid, 'is_private': True}
    return {
        'id': mid,
        'is_private': False,
        'filepath': os.path.basename(filepath),
        'download_name': strip_enc(os.path.basename(filepath)),
        'tags': tags or '',
        'uploaded_by': get_username_by_id(owner) or 'Unknown',
    }


@media_bp.route('/api/upload', methods=['POST'])
@csrf.exempt
@limiter.limit("120 per minute")
def api_upload():
    """Multipart upload returning a per-file JSON result.

    Fields: `file` (repeatable; `files` also accepted), `tags` and
    `private` applied to every file, `tags_<i>` / `private_<i>` overriding
    them per file, and `uploaded_for` for delegated attribution (honored
    only for DELEGATED_UPLOADERS accounts, same rule as the web form).

    Each result carries `status` ('ok' | 'duplicate' | 'error'), the
    content hash, and — for a duplicate — `duplicate_of`, describing the
    item already holding that hash.
    """
    user = _api_user()
    if user is None:
        return jsonify({'error': 'authentication required'}), 401, {
            'WWW-Authenticate': 'Basic realm="MediaIndex API"'}
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({'error': 'no file in the request'}), 400
    delegate = (request.form.get('uploaded_for') or '').strip()
    uid = _owner_uid(user['id'], user['username'], delegate)
    # _owner_uid keeps the uploader as owner when the named user does not
    # exist. A private item is encrypted for its owner alone, so that
    # fallback would file it, unreadable to the person it was meant for,
    # in the bot account's library — refuse it instead.
    unknown_owner = (delegate
                     and user['username'] in Config.DELEGATED_UPLOADERS
                     and get_user_id_by_username(delegate) is None)
    tags = request.form.get('tags', '')
    private = request.form.get('private', '')
    results = []
    for i, f in enumerate(files):
        is_private = request.form.get(f'private_{i}', private) \
            in ('1', 'on', 'true')
        if is_private and unknown_owner:
            results.append({
                'filename': f.filename or '', 'status': 'error',
                'filehash': None,
                'detail': f"no index user named {delegate!r} to own this "
                          f"private item"})
            continue
        status, fh = _store_upload(f, request.form.get(f'tags_{i}', tags),
                                   uid, is_private)
        entry = {'filename': f.filename or '', 'status': status,
                 'filehash': fh}
        if status == 'error':
            entry['detail'] = 'unsupported file type'
        elif status == 'duplicate':
            entry['duplicate_of'] = _duplicate_of(fh)
        results.append(entry)
    return jsonify({'results': results,
                    'uploaded_by': get_username_by_id(uid) or user['username']})


# ── Telegram identity lookup (Telegram bot) ───────────────────────────────
@media_bp.route('/api/telegram/user')
@csrf.exempt
@limiter.limit("120 per minute")
def api_telegram_user():
    """Resolve a Telegram sender to the index account that claimed it.

    The Telegram bot's ingest gate: it calls this before it downloads
    anything, and drops the message when the answer is 404. The bot holds
    no database credentials of its own — Postgres is deliberately confined
    to the `backend` network — so the resolve-and-pin logic runs here,
    where the connection already exists.

    Restricted to DELEGATED_UPLOADERS rather than to any valid app
    password: this maps Telegram identities onto index usernames, which is
    nobody's business but the bot's. An ordinary account with a WebDAV app
    password gets 403.
    """
    user = _api_user()
    if user is None:
        return jsonify({'error': 'authentication required'}), 401, {
            'WWW-Authenticate': 'Basic realm="MediaIndex API"'}
    if user['username'] not in Config.DELEGATED_UPLOADERS:
        return jsonify({'error': 'not permitted'}), 403
    raw_id = (request.args.get('telegram_id') or '').strip()
    try:
        telegram_id = int(raw_id) if raw_id else None
    except ValueError:
        return jsonify({'error': 'telegram_id must be an integer'}), 400
    if telegram_id is None:
        return jsonify({'error': 'telegram_id is required'}), 400
    match = resolve_telegram_user(telegram_id, request.args.get('username'))
    if not match:
        return jsonify({'error': 'not linked'}), 404
    username, pinned = match
    return jsonify({'username': username, 'pinned': pinned})


# ── Discord identity lookup (Discord bot) ─────────────────────────────────
@media_bp.route('/api/discord/user')
@csrf.exempt
@limiter.limit("120 per minute")
def api_discord_user():
    """Resolve a Discord user id to the index account that linked it on
    /settings — the Discord bot's equivalent of api_telegram_user, and
    restricted to DELEGATED_UPLOADERS for the same reason.

    The bot asks before every ingest: the answer decides who owns a channel
    upload, and whether a DM may be stored privately at all.
    """
    refused = _refuse_non_delegated()
    if refused:
        return refused
    discord_id = _discord_id_arg(request.args.get('discord_id'))
    if discord_id is None:
        return jsonify({'error': 'discord_id must be a Discord user id'}), 400
    username = resolve_discord_user(discord_id)
    if not username:
        return jsonify({'error': 'not linked'}), 404
    return jsonify({'username': username})


@media_bp.route('/api/discord/verify', methods=['POST'])
@csrf.exempt
@limiter.limit("30 per minute")
def api_discord_verify():
    """Redeem a link code the user DMed to the Discord bot.

    `discord_id` is the DM's sender as Discord reported it to the bot, and
    `code` is what /settings issued to one logged-in index user — so a
    success proves the same person controls both accounts. 404 for a
    wrong, used or expired code. The per-minute limit is the brute-force
    bound on top of the code's ~40 bits and ten-minute life.
    """
    refused = _refuse_non_delegated()
    if refused:
        return refused
    discord_id = _discord_id_arg(request.form.get('discord_id'))
    if discord_id is None:
        return jsonify({'error': 'discord_id must be a Discord user id'}), 400
    username = claim_discord_link_code(request.form.get('code'), discord_id)
    if not username:
        return jsonify({'error': 'invalid or expired code'}), 404
    return jsonify({'username': username})


# ── Site logins (both bots' yt-dlp) ───────────────────────────────────────
# The cookies an admin stored on /admin/site-logins, handed to the bots one
# login at a time. Same gate as the identity lookups: a stored login is a
# live account session, nobody's business but the bots'.
@media_bp.route('/api/site-logins')
@csrf.exempt
@limiter.limit("120 per minute")
def api_site_logins():
    """Which sites have a login stored — names only, never cookies. The
    Discord bot's startup line reports it."""
    refused = _refuse_non_delegated()
    if refused:
        return refused
    logins = []
    for pid, login in site_logins.all_logins().items():
        info = (site_logins.summary(login.platform, login.text)
                if login.readable else {})
        logins.append({'platform': pid, 'name': login.platform.name,
                       'readable': login.readable,
                       'logged_in': info.get('logged_in', False)})
    return jsonify({'logins': logins})


@media_bp.route('/api/site-logins/cookies')
@csrf.exempt
@limiter.limit("120 per minute")
def api_site_login_cookies():
    """The cookies.txt to download `url` with: its site's own login, else
    the `other` fallback. 404 when neither is stored. `revision` is what a
    write-back of rotated cookies has to name."""
    refused = _refuse_non_delegated()
    if refused:
        return refused
    url = (request.args.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'url is required'}), 400
    login = site_logins.login_for_url(url)
    if login is None:
        return jsonify({'error': 'no login stored for that site'}), 404
    # No-store like every non-media reply (_apply_cache_policy in
    # app/__init__.py):
    # a cached copy of this would be a copy of the session.
    return jsonify({'platform': login.platform.id,
                    'name': login.platform.name,
                    'revision': login.revision,
                    'cookies': login.text})


@media_bp.route('/api/site-logins/<platform_id>/cookies', methods=['PUT'])
@csrf.exempt
@limiter.limit("120 per minute")
def api_site_login_rotate(platform_id):
    """Save what a download rotated a login to.

    JSON `{"revision": …, "cookies": "…"}`. 409 when the login is no longer
    that revision — an admin replaced or removed it meanwhile, and their
    version wins. 422 when the new file would log the site out: a public
    video downloads fine without the session, and keeping that jar would
    throw the login away.
    """
    refused = _refuse_non_delegated()
    if refused:
        return refused
    plat = site_logins.platform(platform_id)
    if plat is None:
        return jsonify({'error': 'unknown site'}), 404
    data = request.get_json(silent=True) or {}
    revision, text = data.get('revision'), data.get('cookies')
    if not isinstance(revision, int) or isinstance(revision, bool) \
            or not isinstance(text, str):
        return jsonify({'error': 'revision and cookies are required'}), 400
    try:
        text, _kept, _dropped = site_logins.clean(plat, text)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    current = site_logins.get_login(plat.id)
    if current is None or current.revision != revision:
        return jsonify({'error': 'the login changed meanwhile'}), 409
    if plat.login_cookies and current.readable \
            and site_logins.summary(plat, current.text)['logged_in'] \
            and not site_logins.summary(plat, text)['logged_in']:
        return jsonify({'error': 'that would log the site out'}), 422
    if text == current.text:
        # Only cookies for other sites changed, and those are never kept.
        return jsonify({'revision': current.revision})
    new_revision = site_logins.save_rotation(plat.id, revision, text)
    if new_revision is None:
        return jsonify({'error': 'the login changed meanwhile'}), 409
    return jsonify({'revision': new_revision})


def _refuse_non_delegated():
    """The error response for a caller that is not a DELEGATED_UPLOADERS
    account authenticated by app password, or None to go ahead."""
    user = _api_user()
    if user is None:
        return jsonify({'error': 'authentication required'}), 401, {
            'WWW-Authenticate': 'Basic realm="MediaIndex API"'}
    if user['username'] not in Config.DELEGATED_UPLOADERS:
        return jsonify({'error': 'not permitted'}), 403
    return None


def _discord_id_arg(raw):
    """A Discord user id from a request field, or None when it is not one
    (it must also fit the bigint column)."""
    raw = (raw or '').strip()
    if not raw.isdigit() or int(raw) >= 2 ** 63:
        return None
    return int(raw)

