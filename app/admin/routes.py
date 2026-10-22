"""Admin panel routes: user management, IP bans, bulk tag operations,
integrity checks, and the log viewer.

The near-duplicate finder's twelve endpoints live in app/admin/dupes_routes.py
— same blueprint, same URLs — because together with everything above they
had made this one module 668 lines across six unrelated concerns.

Every route is guarded by admin_required. All persistence calls go through
app.auth.db / app.media.db — no raw SQL here.
"""
import ipaddress
import os
import shutil
from collections import deque
from datetime import datetime

from flask import (
    render_template, request, redirect, url_for, flash, g, session
)
from werkzeug.security import generate_password_hash

from . import admin_bp
from .format import human_size
from .integrity import (
    scan,
    folder_stats,
    cleanup_missing_rows,
    cleanup_orphan_files,
    cleanup_orphan_thumbs,
    rerender_placeholder_thumbs,
)
from ..auth.routes import (
    MIN_PASSWORD_LENGTH, PASSWORD_RULE, USERNAME_RULE, admin_required, valid_username,
)
from ..auth.db import (
    fetch_all_users,
    fetch_user_by_id,
    insert_user,
    delete_user,
    update_user_password,
    set_user_admin,
    get_user_enc_key,
    get_or_create_user_enc_key,
    fetch_banned_ips,
    fetch_watchlist_ips,
    ban_ip,
    unban_ip,
)
from ..config import Config
from .. import security_log
from ..security_log import log_event
from ..media.staging_db import delete_staging_for_user, staging_thumb_path
from ..media.db import (
    rename_tag,
    remove_tag,
    get_media_count,
    get_tag_statistics,
    get_media_owned_by,
    reassign_media_owner,
    delete_media_for_user,
    delete_media_rows,
    media_path,
    thumb_path,
)


def _overview_stats(users, banned_ips, watchlist):
    media_files, media_bytes = folder_stats(Config.UPLOAD_FOLDER)
    thumb_files, thumb_bytes = folder_stats(Config.THUMB_FOLDER)
    # Free space on the filesystem holding the media files (in Docker that
    # is the bind-mounted host volume, so this reports the host disk).
    disk_free = shutil.disk_usage(Config.UPLOAD_FOLDER).free
    return {
        'users': len(users),
        'media': get_media_count(),
        'tags': len(get_tag_statistics()),
        'storage': human_size(media_bytes + thumb_bytes),
        'storage_detail': f"{media_files} files + {thumb_files} thumbs",
        'storage_free': human_size(disk_free),
        'banned': len(banned_ips),
        'watchlist': len(watchlist),
    }


def _iso_ts(value):
    """Postgres returns timestamptz as datetime; the templates render an
    ISO string."""
    return value.isoformat() if isinstance(value, datetime) else value


def _render_panel(tag_result=None):
    users = fetch_all_users()
    banned_ips = [(ip, count, _iso_ts(ts), manual)
                  for ip, count, ts, manual in fetch_banned_ips()]
    watchlist = [(ip, count, _iso_ts(ts))
                 for ip, count, ts in fetch_watchlist_ips()]
    return render_template(
        'admin.html',
        users=users,
        banned_ips=banned_ips,
        watchlist=watchlist,
        stats=_overview_stats(users, banned_ips, watchlist),
        ban_threshold=Config.BAN_THRESHOLD,
        tag_result=tag_result,
    )


@admin_bp.route('/')
@admin_required
def panel():
    return _render_panel()


# ── User management ────────────────────────────────────────────────────────
@admin_bp.route('/users/create', methods=('POST',))
@admin_required
def create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if not username or not password:
        flash('Username and password are required.', 'danger')
    elif not valid_username(username):
        flash(USERNAME_RULE, 'danger')
    elif len(password) < MIN_PASSWORD_LENGTH:
        flash(PASSWORD_RULE, 'danger')
    else:
        pw_hash = generate_password_hash(password)
        from ..media.crypto import new_key
        # The admin knows this password, so the user replaces it with their
        # own at first login.
        if insert_user(username, pw_hash, new_key(), must_change_password=True):
            log_event('user_created', user=username, by=g.user['username'],
                      via='admin')
            flash(f'User "{username}" created. They will choose their own '
                  'password when they first log in.', 'success')
        else:
            flash('Username already exists.', 'warning')
    return redirect(url_for('admin.panel'))


def _reencrypt_file(path, old_key, new_key):
    """Re-encrypt an '.enc' file in place under a different user key.

    The decrypted intermediate is written to the non-served scratch dir (not
    beside `path` in UPLOAD_FOLDER/THUMB_FOLDER) so this user's plaintext is
    never momentarily reachable through the serving routes.
    """
    from ..media.crypto import encrypt_file, plaintext_copy
    with plaintext_copy(path, old_key, os.path.basename(path) + '.reenc') as tmp:
        encrypt_file(tmp, path, new_key)


def _reassign_user_media(old_uid, new_uid):
    """Hand old_uid's media to new_uid before deletion.

    Private files are re-encrypted with the new owner's key (the old key is
    deleted with the user, which would otherwise leave them undecryptable).
    Private rows whose key is already missing can never be decrypted again,
    so they are deleted instead of reassigned.
    """
    from ..media.crypto import ENC_SUFFIX

    rows = get_media_owned_by(old_uid)
    private_rows = [r for r in rows if r[3]]
    if private_rows:
        old_key = get_user_enc_key(old_uid)
        if old_key:
            new_key = get_or_create_user_enc_key(new_uid)
            for _, fp, th, _ in private_rows:
                for path in (media_path(fp) if fp else None,
                             thumb_path(th) if th else None):
                    if path and path.endswith(ENC_SUFFIX) and os.path.exists(path):
                        _reencrypt_file(path, old_key, new_key)
        else:
            for mid, fp, th, _ in private_rows:
                for path in (media_path(fp) if fp else None,
                             thumb_path(th) if th else None):
                    if path and os.path.exists(path):
                        os.remove(path)
            delete_media_rows([r[0] for r in private_rows])
    return reassign_media_owner(old_uid, new_uid)


@admin_bp.route('/users/<int:user_id>/delete', methods=('POST',))
@admin_required
def remove_user(user_id):
    if user_id == g.user['id']:
        flash('You cannot delete your own account.', 'warning')
        return redirect(url_for('admin.panel'))
    if not fetch_user_by_id(user_id):
        flash('User not found.', 'danger')
        return redirect(url_for('admin.panel'))

    username = fetch_user_by_id(user_id)[1]
    # What happens to the user's media: 'delete' or 'reassign:<user_id>'.
    action = request.form.get('media_action', 'delete')
    if action.startswith('reassign:'):
        try:
            target_id = int(action.split(':', 1)[1])
        except ValueError:
            target_id = None
        if not target_id or target_id == user_id or not fetch_user_by_id(target_id):
            flash('Invalid reassignment target.', 'danger')
            return redirect(url_for('admin.panel'))
        n = _reassign_user_media(user_id, target_id)
        detail = f'{n} media item(s) reassigned.'
        media = f'{n} given to {fetch_user_by_id(target_id)[1]}'
    else:
        n = delete_media_for_user(user_id)
        detail = f'{n} media item(s) deleted.'
        media = f'{n} deleted'

    # WebDAV staging debris: queue rows, staged thumbnails, and the inbox
    # directory. Without this the scanner re-registers the leftover
    # digit-named directory forever — and on Postgres the staging.user_id
    # FK would make every re-registration fail loudly.
    for thumb in delete_staging_for_user(user_id):
        tpath = staging_thumb_path(thumb)
        if os.path.exists(tpath):
            os.remove(tpath)
    shutil.rmtree(os.path.join(Config.STAGING_DIR, str(user_id)),
                  ignore_errors=True)

    delete_user(user_id)
    log_event('user_deleted', user=username, by=g.user['username'], media=media)
    flash(f'User deleted. {detail}', 'success')
    return redirect(url_for('admin.panel'))


@admin_bp.route('/users/<int:user_id>/password', methods=('POST',))
@admin_required
def change_password(user_id):
    password = request.form.get('password', '')
    own = user_id == g.user['id']
    if len(password) < MIN_PASSWORD_LENGTH:
        flash(PASSWORD_RULE, 'danger')
    # Someone else's new password is one the admin knows, so that user
    # replaces it with their own at their next login.
    elif update_user_password(user_id, generate_password_hash(password),
                              must_change_password=not own):
        # The reset logged out every session of that user — including this
        # one, when an admin resets their own password. Keep the browser
        # that made the change signed in; every other copy stays revoked.
        log_event('password_reset', user=fetch_user_by_id(user_id)[1],
                  by=g.user['username'])
        if own:
            session['gen'] = fetch_user_by_id(user_id)[3]
            flash('Password updated.', 'success')
        else:
            flash('Password updated. They will choose their own when they '
                  'next log in.', 'success')
    else:
        flash('User not found.', 'danger')
    return redirect(url_for('admin.panel'))


@admin_bp.route('/users/<int:user_id>/admin', methods=('POST',))
@admin_required
def toggle_admin(user_id):
    # An admin can never change their own status, so every change is made by
    # a *different* admin — guaranteeing at least one admin always remains.
    if user_id == g.user['id']:
        flash('You cannot change your own admin status.', 'warning')
        return redirect(url_for('admin.panel'))
    make_admin = request.form.get('make_admin') == '1'
    if set_user_admin(user_id, make_admin):
        log_event('admin_granted' if make_admin else 'admin_revoked',
                  user=fetch_user_by_id(user_id)[1], by=g.user['username'])
        flash('Admin rights granted.' if make_admin else 'Admin rights revoked.', 'success')
    else:
        flash('User not found.', 'danger')
    return redirect(url_for('admin.panel'))


# ── IP-ban management ──────────────────────────────────────────────────────
@admin_bp.route('/bans/lift', methods=('POST',))
@admin_required
def lift_ban():
    ip = request.form.get('ip', '').strip()
    if ip and unban_ip(ip):
        log_event('ip_unbanned', address=ip, by=g.user['username'])
        flash(f'Ban lifted for {ip}.', 'success')
    else:
        flash('No active ban found for that IP.', 'warning')
    return redirect(url_for('admin.panel'))


@admin_bp.route('/bans/ban', methods=('POST',))
@admin_required
def ban():
    ip = request.form.get('ip', '').strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        flash('Not a valid IP address.', 'danger')
        return redirect(url_for('admin.panel'))
    ban_ip(ip)
    log_event('ip_banned', address=ip, by=g.user['username'], manual=True)
    flash(f'{ip} is now banned.', 'success')
    return redirect(url_for('admin.panel'))


# ── Tag management ─────────────────────────────────────────────────────────
@admin_bp.route('/tags/rename', methods=('POST',))
@admin_required
def tag_rename():
    old = request.form.get('old', '').strip()
    new = request.form.get('new', '').strip()
    dry_run = bool(request.form.get('dry_run'))
    if not old or not new:
        flash('Both the old and the new tag name are required.', 'danger')
        return redirect(url_for('admin.panel'))
    if old == new:
        flash('Old and new tag are identical; nothing to do.', 'warning')
        return redirect(url_for('admin.panel'))

    n, changes = rename_tag(old, new, dry_run)
    verb = 'would update' if dry_run else 'updated'
    flash(f'Rename "{old}" → "{new}": {verb} {n} row(s).', 'success')
    if dry_run:
        return _render_panel(tag_result={
            'action': f'Dry run — rename "{old}" → "{new}"',
            'changes': changes,
        })
    return redirect(url_for('admin.panel'))


@admin_bp.route('/tags/remove', methods=('POST',))
@admin_required
def tag_remove():
    tag = request.form.get('tag', '').strip()
    dry_run = bool(request.form.get('dry_run'))
    if not tag:
        flash('A tag name is required.', 'danger')
        return redirect(url_for('admin.panel'))

    n, changes = remove_tag(tag, dry_run)
    verb = 'would update' if dry_run else 'updated'
    flash(f'Remove "{tag}": {verb} {n} row(s).', 'success')
    if dry_run:
        return _render_panel(tag_result={
            'action': f'Dry run — remove "{tag}"',
            'changes': changes,
        })
    return redirect(url_for('admin.panel'))


# ── Integrity check / orphan cleanup ───────────────────────────────────────
@admin_bp.route('/integrity')
@admin_required
def integrity():
    report = scan()
    return render_template(
        'admin_integrity.html',
        report=report,
        orphan_files_size=human_size(sum(s for _, s in report['orphan_files'])),
        orphan_thumbs_size=human_size(sum(s for _, s in report['orphan_thumbs'])),
    )


@admin_bp.route('/integrity/cleanup', methods=('POST',))
@admin_required
def integrity_cleanup():
    target = request.form.get('target', '')
    if target == 'missing_rows':
        n = cleanup_missing_rows()
        flash(f'Removed {n} database row(s) whose media file was missing.', 'success')
    elif target == 'orphan_files':
        n = cleanup_orphan_files()
        flash(f'Deleted {n} orphaned media file(s).', 'success')
    elif target == 'orphan_thumbs':
        n = cleanup_orphan_thumbs()
        flash(f'Deleted {n} orphaned thumbnail(s).', 'success')
    elif target == 'placeholder_thumbs':
        queued, rerendered, failed = rerender_placeholder_thumbs()
        if queued:
            flash(f'{queued} video(s) queued for a new thumbnail — rescan in a '
                  'minute to see which ones decoded.', 'success')
        if rerendered:
            flash(f'Re-rendered {rerendered} image thumbnail(s).', 'success')
        if failed:
            flash(f'{failed} image(s) still could not be rendered — the file '
                  'itself is probably unreadable.', 'warning')
    else:
        flash('Unknown cleanup target.', 'danger')
    return redirect(url_for('admin.integrity'))


# ── Log viewer ─────────────────────────────────────────────────────────────
_LOG_LINE_CHOICES = (50, 200, 1000)


def _log_files():
    """Available log files, as paths under LOG_FOLDER: the current app.log
    first, then rotated ones, then the security log's days, newest first."""
    try:
        names = [
            f for f in os.listdir(Config.LOG_FOLDER)
            if f.startswith('app.log') and os.path.isfile(os.path.join(Config.LOG_FOLDER, f))
        ]
    except FileNotFoundError:
        names = []
    rotated = sorted((f for f in names if f != 'app.log'), reverse=True)
    try:
        security = sorted(
            (f'security/{f}' for f in os.listdir(security_log.log_dir())
             if f.startswith('security-') and f.endswith('.log')),
            reverse=True)
    except FileNotFoundError:
        security = []
    return (['app.log'] if 'app.log' in names else []) + rotated + security


@admin_bp.route('/logs')
@admin_required
def logs():
    files = _log_files()
    selected = request.args.get('file', 'app.log')
    if selected not in files:  # whitelist against the real listing — no traversal
        selected = files[0] if files else None

    try:
        lines = int(request.args.get('lines', 200))
    except ValueError:
        lines = 200
    if lines not in _LOG_LINE_CHOICES:
        lines = 200
    query = request.args.get('q', '').strip()

    content, error = [], None
    if selected:
        try:
            tail = deque(maxlen=lines)
            with open(os.path.join(Config.LOG_FOLDER, selected), encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if query and query.lower() not in line.lower():
                        continue
                    tail.append(line.rstrip('\n'))
            content = list(tail)
        except OSError as e:
            error = str(e)

    return render_template(
        'admin_logs.html',
        files=files,
        selected=selected,
        lines=lines,
        line_choices=_LOG_LINE_CHOICES,
        query=query,
        content=content,
        error=error,
    )
