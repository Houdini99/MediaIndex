"""WebDAV inbox — a WsgiDAV app mounted at /dav beside the Flask app.

Requests to /dav never enter Flask (DispatcherMiddleware routes them here
first), so CSRF, CSP headers, and the Flask rate limiter don't apply;
authentication and IP-ban throttling live in MediaIndexDomainController.
"""
from ..config import Config
from .dc import MediaIndexDomainController
from .provider import UserStagingProvider


def _limited_input(dav_app):
    """Bound wsgi.input by Content-Length for servers that hand over the raw
    socket (werkzeug's dev server): wsgidav streams PUT bodies until EOF, so
    an unbounded stream blocks until the client times out. Gunicorn already
    terminates the stream (wsgi.input_terminated) and is left untouched."""
    from werkzeug.wsgi import LimitedStream

    def wrapper(environ, start_response):
        if not environ.get('wsgi.input_terminated'):
            try:
                length = int(environ.get('CONTENT_LENGTH') or 0)
            except ValueError:
                length = 0
            environ['wsgi.input'] = LimitedStream(environ['wsgi.input'], length)
        return dav_app(environ, start_response)

    return wrapper


def _lock_storage():
    """DAV lock storage shared across gunicorn workers.

    Locks must be visible to every worker (Windows does LOCK→PUT→UNLOCK and
    the requests can land on different workers), so production uses Redis —
    already part of the stack for rate limiting. A file-backed shelve is NOT
    an option here: gdbm allows only one process to hold it open, so the
    second worker crashes at boot. Without a redis:// URL (local dev, tests)
    fall back to the in-process dict, which is correct for a single process.
    """
    from urllib.parse import urlsplit

    url = urlsplit(Config.REDIS_URL) if Config.REDIS_URL else None
    if url and url.scheme == 'redis':
        from wsgidav.lock_man.lock_storage_redis import LockStorageRedis
        return LockStorageRedis(
            host=url.hostname or '127.0.0.1',
            port=url.port or 6379,
            db=int(url.path.lstrip('/') or 0) if url.path else 0,
            password=url.password,
        )
    from wsgidav.lock_man.lock_storage import LockStorageDict
    return LockStorageDict()


def build_dav_app():
    # Imported lazily so the app can start without wsgidav when DAV_ENABLED
    # is off (e.g. stripped-down deployments).
    from wsgidav.wsgidav_app import WsgiDAVApp

    return _limited_input(WsgiDAVApp({
        "provider_mapping": {"/": UserStagingProvider(Config.STAGING_DIR)},
        "http_authenticator": {
            "domain_controller": MediaIndexDomainController,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        # hrefs are built from mount_path (wsgidav ignores SCRIPT_NAME for
        # this), so it must match the DispatcherMiddleware mount point.
        "mount_path": "/dav",
        "verbose": 1,
        # No dead properties needed for a dump folder.
        "property_manager": None,
        "lock_storage": _lock_storage(),
        "dir_browser": {"enable": True},
        "suppress_version_info": True,
    }))
