"""Admin pages for the site logins the bots download with.

Same blueprint and guard as the rest of the admin panel. The logic —
filtering, encryption, revisions — lives in app/site_logins.py;
this only turns forms into calls and results into flashes.
"""
import logging
from datetime import datetime, timezone

from flask import flash, g, redirect, render_template, request, url_for

from . import admin_bp
from .. import site_logins as sl
from ..auth.routes import admin_required


def _when(value):
    """'2026-10-20 14:03 UTC' for a timestamptz (or unix time), '' for None."""
    if value is None:
        return ''
    if not isinstance(value, datetime):
        value = datetime.fromtimestamp(value, timezone.utc)
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


@admin_bp.route('/site-logins')
@admin_required
def site_logins():
    stored = sl.all_logins()
    rows = []
    for plat in sl.all_platforms():
        login = stored.get(plat.id)
        info = sl.summary(plat, login.text) if login and login.readable \
            else None
        rows.append({
            'platform': plat,
            'login': login,
            'info': info,
            'expires': _when(info['expires']) if info else '',
            'uploaded_at': _when(login.uploaded_at) if login else '',
            'rotated_at': _when(login.rotated_at) if login else '',
        })
    return render_template('admin_site_logins.html', rows=rows,
                           max_kb=sl.MAX_BYTES // 1024)


def _platform_or_404(platform_id):
    plat = sl.platform(platform_id)
    if plat is None:
        flash('Unknown site.', 'danger')
    return plat


@admin_bp.route('/site-logins/<platform_id>/upload', methods=('POST',))
@admin_required
def site_login_upload(platform_id):
    plat = _platform_or_404(platform_id)
    if plat is None:
        return redirect(url_for('admin.site_logins'))
    upload = request.files.get('cookies')
    raw = upload.read(sl.MAX_BYTES + 1) if upload else b''
    if not raw:
        flash('Choose a cookies.txt file to upload.', 'danger')
        return redirect(url_for('admin.site_logins'))
    try:
        text, kept, dropped = sl.clean(plat,
                                       raw.decode('utf-8', errors='replace'))
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('admin.site_logins'))
    sl.save_upload(plat.id, text, g.user['id'])
    logging.info("site-logins: %s uploaded the %s login (%d cookies, %d "
                 "for other sites dropped)", g.user['username'], plat.id,
                 kept, dropped)
    note = f' ({dropped} for other sites were left out)' if dropped else ''
    flash(f'Saved the {plat.name} login: {kept} cookie(s){note}.', 'success')
    if plat.login_cookies and not sl.summary(plat, text)['logged_in']:
        flash(f"None of {plat.name}'s login cookies "
              f"({', '.join(plat.login_cookies)}) are in that file, so "
              f"downloads will still run logged out. Export it again while "
              f"logged in.", 'warning')
    return redirect(url_for('admin.site_logins'))


@admin_bp.route('/site-logins/<platform_id>/remove', methods=('POST',))
@admin_required
def site_login_remove(platform_id):
    plat = _platform_or_404(platform_id)
    if plat is None:
        return redirect(url_for('admin.site_logins'))
    if sl.delete_login(plat.id):
        logging.info("site-logins: %s removed the %s login",
                     g.user['username'], plat.id)
        flash(f'Removed the {plat.name} login.', 'success')
    else:
        flash(f'No {plat.name} login was stored.', 'warning')
    return redirect(url_for('admin.site_logins'))
