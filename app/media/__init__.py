"""Media blueprint package."""
from flask import Blueprint

media_bp = Blueprint('media', __name__)

from . import routes  # noqa: E402,F401
from . import staging_routes  # noqa: E402,F401
