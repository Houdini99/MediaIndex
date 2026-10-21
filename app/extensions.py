"""Flask extensions instantiated without an app — bound inside the factory."""
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from .config import Config

csrf = CSRFProtect()

# Config, not os.environ: this module read the variable directly while every
# other consumer went through Config, so the two could drift apart silently.
# app/__init__.py imports .config before .extensions, so Config is always
# populated by the time this runs.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=Config.REDIS_URL,
)
