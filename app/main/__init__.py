"""Main blueprint — small static-ish endpoints (favicon, robots)."""
from flask import Blueprint

main_bp = Blueprint('main', __name__)

from . import routes  # noqa: E402,F401
