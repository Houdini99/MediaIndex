"""Admin blueprint package."""
from flask import Blueprint

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

from . import routes  # noqa: E402,F401  (registers routes on admin_bp)
from . import dupes_routes  # noqa: E402,F401  (same, for the duplicate finder)
from . import site_logins_routes  # noqa: E402,F401  (same, for site logins)
