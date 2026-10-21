"""Admin → Near Duplicates: the scan, the result grid, and resolving.

Split out of app/admin/routes.py, which had grown to hold user management,
IP bans, tag operations, the integrity check, the log viewer *and* these
twelve endpoints. Every route still registers on
the same `admin_bp` and keeps its URL and endpoint name, so `url_for` and
every template and fetch() call are untouched.

The engine lives in app/admin/dupes.py; this module is the HTTP surface
over it — parameter parsing, pagination, and JSON/CSV shaping.
"""
import csv
import io

from flask import (
    render_template, request, redirect, url_for, flash, g, jsonify, Response
)

from . import admin_bp
from . import dupes
from .format import human_size
from ..auth.routes import admin_required
from ..media.dupes_db import (
    fetch_dismissed_detail,
    fetch_quarantine,
    quarantine_summary,
)


# The page renders the shell (scan form, quarantine, dismissed pairs); the
# result grid and the progress bar are driven by the JSON endpoints below,
# because a scan outlives any single request.
_GROUPS_PER_PAGE = 20


def _dupe_params():
    """Scan/grouping parameters from the query string, normalized."""
    return dupes.normalize_params({
        'source': request.args.get('source'),
        'algo': request.args.get('algo'),
        'threshold': request.args.get('threshold'),
        'types': request.args.get('types'),
        'video_frames': request.args.get('video_frames'),
        'min_group': request.args.get('min_group'),
        'cross_uploader_only': request.args.get('cross_uploader_only'),
        'sort': request.args.get('sort'),
    })


def _json_ids(data, key, limit=500):
    """Validated list of media ids from a JSON body, or None."""
    ids = data.get(key)
    if not isinstance(ids, list) or not ids:
        return None
    try:
        return [int(i) for i in ids[:limit]]
    except (TypeError, ValueError):
        return None


@admin_bp.route('/duplicates')
@admin_required
def duplicates():
    params = _dupe_params()
    q_count, q_bytes = quarantine_summary()
    return render_template(
        'admin_duplicates.html',
        params=params,
        status=dupes.scan_status(),
        scale=dupes.library_scale(),
        quarantine=fetch_quarantine(),
        quarantine_count=q_count,
        quarantine_size=human_size(q_bytes or 0),
        dismissed=fetch_dismissed_detail(),
        errors=dupes.hash_errors(),
        sources=dupes.SOURCES,
        algos=dupes.ALGOS,
        types=dupes.TYPES,
        frame_choices=dupes.VIDEO_FRAME_CHOICES,
        max_threshold=dupes.MAX_THRESHOLD,
    )


@admin_bp.route('/duplicates/scan', methods=('POST',))
@admin_required
def duplicates_scan():
    params = dupes.normalize_params({
        'source': request.form.get('source'),
        'algo': request.form.get('algo'),
        'threshold': request.form.get('threshold'),
        'types': ','.join(request.form.getlist('types')),
        'video_frames': request.form.get('video_frames'),
        'min_group': request.form.get('min_group'),
        'rehash': request.form.get('rehash'),
        'sort': request.form.get('sort'),
    })
    job_id, message = dupes.start_scan(params, g.user['id'])
    flash(message, 'success' if job_id else 'warning')
    return redirect(url_for('admin.duplicates', **_dupe_query(params)))


def _dupe_query(params):
    """Parameters as query-string arguments, so the page keeps its view."""
    return {
        'source': params['source'],
        'algo': params['algo'],
        'threshold': params['threshold'],
        'types': ','.join(params['types']),
        'video_frames': params['video_frames'],
        'min_group': params['min_group'],
        'sort': params['sort'],
    }


@admin_bp.route('/duplicates/cancel', methods=('POST',))
@admin_required
def duplicates_cancel():
    if dupes.cancel_scan():
        return jsonify({'cancelled': True})
    return jsonify({'cancelled': False, 'error': 'no scan is running'}), 409


@admin_bp.route('/duplicates/status')
@admin_required
def duplicates_status():
    return jsonify(dupes.scan_status())


@admin_bp.route('/duplicates/groups')
@admin_required
def duplicates_groups():
    params = _dupe_params()
    result = dupes.find_groups(params)
    groups = result['groups']
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    start = (page - 1) * _GROUPS_PER_PAGE
    window = groups[start:start + _GROUPS_PER_PAGE]
    for group in window:
        # Formatted here rather than in the browser: the card shows this
        # figure directly beside the server-formatted item['size_h'], and a
        # second implementation in JS could only ever drift away from this
        # one.
        group['wasted_h'] = human_size(group['wasted'])
        for item in group['items']:
            item['thumb_url'] = (
                url_for('media.serve_thumb', fname=item['thumb'])
                if item['thumb'] else None)
            item['media_url'] = url_for('media.serve_media', fname=item['filepath'])
            item['size_h'] = human_size(item['bytes'] or 0)
    return jsonify({
        'groups': window,
        'page': page,
        'pages': max(1, -(-len(groups) // _GROUPS_PER_PAGE)),
        'has_more': start + _GROUPS_PER_PAGE < len(groups),
        'stats': {**result['stats'],
                  'wasted_h': human_size(result['stats']['wasted'])},
        'truncated': result['truncated'],
        'params': params,
    })


@admin_bp.route('/duplicates/resolve', methods=('POST',))
@admin_required
def duplicates_resolve():
    data = request.get_json(silent=True) or {}
    ids = _json_ids(data, 'ids')
    try:
        keep_id = int(data.get('keep_id'))
    except (TypeError, ValueError):
        keep_id = None
    if not ids or keep_id is None:
        return jsonify({'error': 'keep_id and a non-empty ids list are required'}), 400
    if keep_id not in ids:
        return jsonify({'error': 'keep_id must be one of ids'}), 400
    quarantined, failed = dupes.quarantine_group(
        keep_id, [i for i in ids if i != keep_id],
        bool(data.get('merge_tags')), g.user['id'], data.get('distance'))
    return jsonify({'quarantined': quarantined, 'failed': failed,
                    'keep_id': keep_id})


@admin_bp.route('/duplicates/resolve-all', methods=('POST',))
@admin_required
def duplicates_resolve_all():
    """Apply the suggested keeper to every group matching the current view.

    Always previewed first: the UI sends dry_run=true, shows the count, and
    only re-sends with dry_run=false once the admin confirms.
    """
    data = request.get_json(silent=True) or {}
    params = dupes.normalize_params(data.get('params') or {})
    groups = dupes.find_groups(params)['groups']
    result = dupes.resolve_all(groups, g.user['id'],
                               merge_tags=bool(data.get('merge_tags')),
                               dry_run=data.get('dry_run', True) is not False)
    result['bytes_h'] = human_size(result['bytes'])
    return jsonify(result)


@admin_bp.route('/duplicates/dismiss', methods=('POST',))
@admin_required
def duplicates_dismiss():
    data = request.get_json(silent=True) or {}
    ids = _json_ids(data, 'ids')
    if not ids or len(ids) < 2:
        return jsonify({'error': 'at least two ids are required'}), 400
    return jsonify({'dismissed': dupes.dismiss_group(ids, g.user['id'])})


@admin_bp.route('/duplicates/undismiss', methods=('POST',))
@admin_required
def duplicates_undismiss():
    pairs = []
    for raw in request.form.getlist('pair'):
        try:
            a, b = raw.split(':', 1)
            pairs.append((int(a), int(b)))
        except ValueError:
            continue
    n = dupes.undismiss(pairs) if pairs else 0
    flash(f'{n} pair(s) restored — they can match again.'
          if n else 'No pairs were restored.',
          'success' if n else 'warning')
    return redirect(url_for('admin.duplicates'))


@admin_bp.route('/duplicates/quarantine/restore', methods=('POST',))
@admin_required
def duplicates_quarantine_restore():
    if request.form.get('all') == '1':
        ids = None
    else:
        ids = [int(i) for i in request.form.getlist('media_id') if i.isdigit()]
        if not ids:
            flash('Nothing selected.', 'warning')
            return redirect(url_for('admin.duplicates'))
    n = dupes.restore_quarantine(ids, g.user['id'])
    flash(f'{n} item(s) restored; their original tags are back.', 'success')
    return redirect(url_for('admin.duplicates'))


@admin_bp.route('/duplicates/quarantine/purge', methods=('POST',))
@admin_required
def duplicates_quarantine_purge():
    """Step two of the resolve — this is the only destructive action here."""
    if request.form.get('all') == '1':
        ids = None
    else:
        ids = [int(i) for i in request.form.getlist('media_id') if i.isdigit()]
        if not ids:
            flash('Nothing selected.', 'warning')
            return redirect(url_for('admin.duplicates'))
    deleted, failed, freed = dupes.purge_quarantine(ids, g.user['id'])
    if deleted:
        flash(f'{deleted} item(s) permanently deleted — {human_size(freed)} freed.',
              'success')
    if failed:
        flash(f'{failed} item(s) could not be deleted.', 'danger')
    return redirect(url_for('admin.duplicates'))


@admin_bp.route('/duplicates/export.csv')
@admin_required
def duplicates_export():
    params = _dupe_params()
    groups = dupes.find_groups(params)['groups']
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['group', 'media_id', 'keeper', 'similarity', 'type',
                     'width', 'height', 'bytes', 'uploader', 'upload_date',
                     'filename', 'tags'])
    for group in groups:
        for item in group['items']:
            writer.writerow([
                group['key'], item['id'],
                'keep' if item['id'] == group['keeper_id'] else 'duplicate',
                group['similarity'], item['ftype'], item['width'], item['height'],
                item['bytes'], item['uploader'], item['upload_date'],
                item['filepath'], item['tags'],
            ])
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=near-duplicates.csv'},
    )
