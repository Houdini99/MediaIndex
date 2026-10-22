"""Application factory."""
import os
from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from .config import Config
from .extensions import csrf, limiter
from .proxy import DEFAULT_TRUSTED, TrustedProxyFix, parse_networks


def create_app():
    Config.validate()

    app = Flask(__name__, static_folder='static', template_folder='templates')

    app.config.from_object(Config)

    # ── Ensure upload/thumb/data/staging directories exist ─────────────────
    for folder in (Config.UPLOAD_FOLDER, Config.THUMB_FOLDER, Config.DATA_DIR,
                   Config.STAGING_DIR, Config.STAGING_THUMB_DIR):
        os.makedirs(folder, exist_ok=True)

    # ── Initialise databases ──────────────────────────────────────────────
    from .auth.db import init_auth_db
    from .media.db import init_media_db
    init_auth_db()
    init_media_db()

    # ── Middleware stack (order matters) ───────────────────────────────────
    # DispatcherMiddleware routes /dav to the WsgiDAV app before Flask sees
    # the request; ProxyFix wraps the WHOLE stack so both branches see the
    # real client IP (the DAV domain controller needs it for ban/throttle).
    if Config.DAV_ENABLED:
        from .dav import build_dav_app
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {'/dav': build_dav_app()})
    # ProxyFix runs only for requests whose peer is a trusted proxy; anyone
    # else keeps their real REMOTE_ADDR and loses the forwarded headers, so
    # the ban system and rate limiter can't be fed a spoofed address by a
    # client that reaches the app without going through NPM.
    trusted = parse_networks(Config.TRUSTED_PROXIES) or \
        parse_networks(','.join(DEFAULT_TRUSTED))
    app.wsgi_app = TrustedProxyFix(
        app.wsgi_app,
        trusted,
        x_for=1,    # Trust 1 proxy (NPM)
        x_proto=1,
        x_host=1,
        x_prefix=1
    )

    # ── Bind extensions ───────────────────────────────────────────────────
    csrf.init_app(app)
    limiter.init_app(app)

    # ── Register blueprints ───────────────────────────────────────────────
    from .auth import auth_bp
    from .media import media_bp
    from .main import main_bp
    from .admin import admin_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)

    # ── WebDAV staging scanner (one instance across gunicorn workers) ──────
    if Config.STAGING_SCANNER_ENABLED:
        from .media.staging import start_scanner
        start_scanner()

    # ── Template filter ───────────────────────────────────────────────────
    @app.template_filter('basename')
    def basename_filter(path):
        return os.path.basename(path)

    # ── Security headers ──────────────────────────────────────────────────
    @app.after_request
    def add_security_headers(response):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains; preload'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "base-uri 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "connect-src 'self'; "
            "font-src 'self';"
        )
        _apply_cache_policy(response)
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        # Explicitly OFF, not omitted. The legacy XSS auditor was removed
        # from Chrome and Edge and was itself a source of
        # information-disclosure bugs; `1; mode=block` asks the few engines
        # that still honour it to do something actively unwanted. The real
        # defence is the CSP above, which doesn't permit inline script.
        response.headers['X-XSS-Protection'] = '0'
        return response

    return app


#: Endpoints serving content-addressed, immutable bytes: the stored name is
#: `<sha256>.<ext>` (plus `.enc` when private), so a given URL can only ever
#: return the same bytes. Editing tags or flipping privacy does not mutate a
#: file — a privacy flip renames it, which changes the URL.
#:
#: Deliberately excludes the staging endpoints (`media.staging_thumb`,
#: `media.staging_file`): those are keyed by row id, not content hash, and a
#: WebDAV client overwriting a queued file reuses the same id.
IMMUTABLE_ENDPOINTS = frozenset({'media.serve_media', 'media.serve_thumb'})

#: One year, the maximum worth expressing. `private` keeps these out of any
#: shared cache, which matters because the same routes serve other users'
#: private media to their owner.
IMMUTABLE_CACHE = 'private, max-age=31536000, immutable'


def _apply_cache_policy(response):
    """`no-store` for pages, long-lived caching for immutable media.

    The blanket `no-store` this replaces was applied to *every* response,
    including `/media/<fname>` and `/thumbs/<fname>` — 87 MB of thumbnails
    and 54 GB of media that are immutable by construction were re-fetched on
    every navigation, and every video scrub re-downloaded ranges the browser
    already had.

    Only successful bodies are cached: an error is a transient condition, and
    `304 Not Modified` carries no body to cache. `Vary: Cookie` is already
    set by Flask wherever the session is touched, so a cached response cannot
    leak across accounts.
    """
    from flask import request
    if (request.endpoint in IMMUTABLE_ENDPOINTS
            and response.status_code in (200, 206)):
        response.headers['Cache-Control'] = IMMUTABLE_CACHE
        # Werkzeug's send_file sets `no-cache` when no max_age is given.
        response.headers.pop('Pragma', None)
        return
    # `Pragma: no-cache` is deliberately not set: it is an HTTP/1.0 *request*
    # header and means nothing in a response. `Cache-Control` covers every
    # cache written since 1997.
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
