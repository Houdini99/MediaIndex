"""Misc top-level routes (favicon, robots)."""
from flask import current_app, send_from_directory

from . import main_bp


@main_bp.route('/favicon.ico')
def favicon():
    return send_from_directory(
        current_app.static_folder,
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )


@main_bp.route('/robots.txt')
def robots():
    return "User-agent: *\nDisallow: /\n", 200, {'Content-Type': 'text/plain'}
