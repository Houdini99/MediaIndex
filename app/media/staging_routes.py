"""Untagged-queue pages, the user settings page, and JSON APIs for the
WebDAV staging inbox.

All persistence goes through app.media.staging_db; the promote step reuses
the shared ingest pipeline (app.media.ingest). Every disk path derived from
a staging row is re-validated to sit inside the requesting user's own
staging directory before it is served, moved, or deleted.
"""
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
from flask import (
    render_template, request, jsonify, g, abort, send_file, url_for
)

from . import media_bp
from . import staging_db
from .ingest import ingest_file
from ..auth.db import (
    DISCORD_LINK_TTL,
    discord_link_pending,
    get_dav_password_created,
    get_discord_identity,
    get_telegram_identity,
    hash_dav_password,
    new_discord_link_code,
    set_dav_password_hash,
    set_discord_identity,
    set_telegram_identity,
)
from ..auth.routes import login_required
from ..config import Config
from ..extensions import limiter
from ..request_parsing import form_flag
from ..security_log import log_event


def _own_staging_path(row):
    """Absolute, validated path of the row's file inside the requesting
    user's staging jail; aborts if the stored fname escapes it."""
    user_root = os.path.realpath(os.path.join(Config.STAGING_DIR, str(g.user['id'])))
    path = os.path.realpath(staging_db.staging_file_path(g.user['id'], row['fname']))
    if os.path.commonpath([user_root, path]) != user_root:
        abort(404)
    return path


def _remove_staging_thumb(row):
    if row.get('thumb'):
        tpath = staging_db.staging_thumb_path(row['thumb'])
        if os.path.exists(tpath):
            os.remove(tpath)


def _row_json(row):
    return {
        'id': row['id'],
        'fname': row['fname'],
        'size': row['size'],
        'ftype': row['ftype'],
        'status': row['status'],
        'discovered_at': row['discovered_at'],
        'thumb_url': url_for('media.staging_thumb', sid=row['id']) if row['thumb'] else None,
        'file_url': url_for('media.staging_file', sid=row['id']),
    }


# ── Badge count for the nav (every page render) ────────────────────────────
@media_bp.app_context_processor
def inject_staging_count():
    user = getattr(g, 'user', None)
    if not user:
        return {}
    try:
        return {'staging_count': staging_db.count_pending(user['id'])}
    except Exception:
        return {}


# ── Pages ─────────────────────────────────────────────────────────────────
@media_bp.route('/queue')
@login_required
def queue():
    return render_template(
        'queue.html',
        dav_url=request.url_root + 'dav/',
        dav_configured=bool(get_dav_password_created(g.user['id'])),
    )


@media_bp.route('/settings')
@login_required
def settings():
    """Per-user settings (the cogwheel tab). Account-level knobs live here
    rather than on /queue, which is about the files waiting in the inbox."""
    tg_id, tg_name = get_telegram_identity(g.user['id'])
    return render_template(
        'settings.html',
        telegram_id=tg_id,
        telegram_username=tg_name,
        telegram_bot=Config.TELEGRAM_BOT_NAME,
        discord_id=get_discord_identity(g.user['id']),
    )


# ── JSON APIs ──────────────────────────────────────────────────────────────
@media_bp.route('/api/staging')
@login_required
def api_staging_list():
    rows = staging_db.list_staging(g.user['id'])
    return jsonify({'items': [_row_json(r) for r in rows]})


@media_bp.route('/staging/thumb/<int:sid>')
@login_required
def staging_thumb(sid):
    row = staging_db.get_staging_row(sid, g.user['id'])
    if not row or not row['thumb']:
        abort(404)
    tpath = staging_db.staging_thumb_path(row['thumb'])
    if not os.path.exists(tpath):
        abort(404)
    return send_file(tpath, mimetype='image/png')


@media_bp.route('/staging/file/<int:sid>')
@login_required
def staging_file(sid):
    row = staging_db.get_staging_row(sid, g.user['id'])
    if not row:
        abort(404)
    path = _own_staging_path(row)
    if not os.path.isfile(path):
        abort(404)
    mime = mimetypes.guess_type(row['fname'])[0] or 'application/octet-stream'
    return send_file(path, mimetype=mime, conditional=True)


@media_bp.route('/api/staging/<int:sid>/promote', methods=['POST'])
@login_required
def staging_promote(sid):
    row = staging_db.get_staging_row(sid, g.user['id'])
    if not row:
        abort(404)
    if not staging_db.claim_for_promote(sid, g.user['id']):
        return jsonify({'error': 'not ready'}), 409
    try:
        path = _own_staging_path(row)
        if not os.path.isfile(path):
            # Deleted via DAV after the queue page loaded.
            _remove_staging_thumb(row)
            staging_db.delete_staging_row(sid)
            return jsonify({'error': 'file no longer in WebDAV folder'}), 410
        # Guard against promoting a file that is still being written: a
        # stalled DAV transfer can look size-stable long enough for the
        # scanner to mark it ready, but moving it out from under the open
        # write handle would silently truncate it. If the file no longer
        # matches the scanned snapshot, hand it back to the scanner.
        st = os.stat(path)
        if st.st_size != row['size'] or st.st_mtime != row['mtime']:
            staging_db.release_claim(sid)
            return jsonify({'error': 'file is still being uploaded'}), 409
        tags = request.form.get('tags', '')
        is_private = form_flag(request.form.get('private'))
        ext = os.path.splitext(row['fname'])[1].lstrip('.').lower()
        # Deliver the file into UPLOAD_FOLDER first (shutil.move survives
        # the staging→media_files Docker bind-mount boundary), then hand it
        # to the shared pipeline.
        fd, tmp = tempfile.mkstemp(dir=Config.UPLOAD_FOLDER, suffix=f'.{ext}')
        os.close(fd)
        shutil.move(path, tmp)
        # Reuse the scanner's already-rendered thumbnail (same source file,
        # same size) instead of running Pillow/ffmpeg a second time.
        premade = None
        if row['thumb']:
            tpath = staging_db.staging_thumb_path(row['thumb'])
            if os.path.exists(tpath):
                premade = tpath
        status, _fh = ingest_file(tmp, ext, tags, g.user['id'], is_private,
                                  premade_thumb=premade)
        # Either way the file has left staging: clear the queue entry.
        _remove_staging_thumb(row)
        staging_db.delete_staging_row(sid)
        return jsonify({'status': status})
    except Exception:
        staging_db.release_claim(sid, status=row['status'])
        raise


@media_bp.route('/api/staging/<int:sid>/reject', methods=['POST'])
@login_required
def staging_reject(sid):
    row = staging_db.get_staging_row(sid, g.user['id'])
    if not row:
        abort(404)
    path = _own_staging_path(row)
    if os.path.isfile(path):
        os.remove(path)
    _remove_staging_thumb(row)
    staging_db.delete_staging_row(sid)
    return jsonify({'status': 'rejected'})


# ── WebDAV app-password management ─────────────────────────────────────────
@media_bp.route('/api/dav/password', methods=['POST'])
@login_required
@limiter.limit("5 per hour")
def dav_password_generate():
    password = secrets.token_urlsafe(24)
    set_dav_password_hash(g.user['id'], hash_dav_password(password))
    log_event('app_password_created', user=g.user['username'])
    return jsonify({
        'password': password,
        'username': g.user['username'],
        'dav_url': request.url_root + 'dav/',
    })


@media_bp.route('/api/dav/password/revoke', methods=['POST'])
@login_required
def dav_password_revoke():
    set_dav_password_hash(g.user['id'], None)
    log_event('app_password_revoked', user=g.user['username'])
    return jsonify({'status': 'revoked'})
# ── Telegram account linking ───────────────────────────────────────────────
# The other half of this lives in app/media/routes.py:api_telegram_user(),
# which is what the bot actually calls. This end is the user-facing form
# on /settings.
@media_bp.route('/api/telegram/link', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def telegram_link():
    """Claim a Telegram account for the logged-in user.

    Accepts one field, `identity`, holding either the numeric Telegram
    user id or an @username — the numeric id is the safer of the two (see
    resolve_telegram_user), so an all-digit value is stored as the id and
    anything else as the username. Empty unlinks.

    Storing one always clears the other, which is what makes re-linking to
    a different Telegram account work: leaving a stale pinned id behind
    would keep matching the old account no matter what the form said.
    """
    raw = (request.form.get('identity') or '').strip().lstrip('@')
    if not raw:
        set_telegram_identity(g.user['id'], None, None)
        return jsonify({'status': 'unlinked'})
    if raw.isdigit():
        tg_id, tg_name = int(raw), None
    else:
        # Telegram's own rule: 5-32 chars, letters/digits/underscore.
        # Rejected here rather than stored, because a value that can never
        # match a real sender is indistinguishable from a working link on
        # the settings page — it just silently never ingests.
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', raw):
            return jsonify({
                'error': 'That is neither a numeric Telegram ID nor a valid '
                         '@username (5-32 letters, digits or underscores).'
            }), 400
        tg_id, tg_name = None, raw
    if set_telegram_identity(g.user['id'], tg_id, tg_name) == 'taken':
        return jsonify({
            'error': 'Another index account has already claimed that '
                     'Telegram account.'
        }), 409
    return jsonify({
        'status': 'linked',
        'telegram_id': tg_id,
        'telegram_username': tg_name,
        'pinned': tg_id is not None,
    })


# ── Discord account linking ────────────────────────────────────────────────
# Linking is a round trip through Discord itself: this page issues a code,
# the user DMs it to the bot, and the bot redeems it with the sender's id
# (app/media/routes.py:api_discord_verify). Nothing here accepts an id.
@media_bp.route('/api/discord/link/code', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def discord_link_code():
    """Issue a one-time link code for the logged-in user (replacing any
    earlier one)."""
    return jsonify({'code': new_discord_link_code(g.user['id']),
                    'expires_in': DISCORD_LINK_TTL})


@media_bp.route('/api/discord/unlink', methods=['POST'])
@login_required
def discord_unlink():
    set_discord_identity(g.user['id'], None)
    return jsonify({'status': 'unlinked'})


@media_bp.route('/api/discord/status')
@login_required
def discord_status():
    """What the settings page polls while a code is on screen: the linked
    id (a string — 19 digits do not survive being a JavaScript number) and
    whether the code is still waiting to be redeemed."""
    discord_id = get_discord_identity(g.user['id'])
    return jsonify({'discord_id': str(discord_id) if discord_id else None,
                    'pending': discord_link_pending(g.user['id'])})
