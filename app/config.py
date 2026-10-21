"""Environment-driven configuration.

All values are read from environment variables — no hardcoded secrets,
no standalone config dictionary checked into the repo.
"""
import os
from datetime import timedelta

# Project root: directory containing run.py and the app/ package.
# /app/app/config.py → dirname = /app/app → dirname = /app
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Config:
    # Filesystem locations — absolute, anchored at PROJECT_ROOT so they don't
    # depend on the process working directory or Flask's app.root_path.
    UPLOAD_FOLDER = os.path.join(PROJECT_ROOT, 'media_files')
    THUMB_FOLDER = os.path.join(PROJECT_ROOT, 'thumbnails')
    # data/ hosts cross-worker runtime files (staging_scanner.lock)
    # — the databases themselves live in PostgreSQL, and DAV lock storage
    # is Redis-backed (app/dav/__init__.py), never a file.
    DATA_DIR = os.path.join(PROJECT_ROOT, 'data')
    # PostgreSQL — one database, schemas `media` and `users`. Required.
    DATABASE_URL = os.environ.get('DATABASE_URL', '')
    LOG_FOLDER = os.path.join(PROJECT_ROOT, 'log')
    # WebDAV inbox: each user gets staging/<user_id>/ as their personal DAV
    # root; staged thumbs live inside THUMB_FOLDER so they ride the existing
    # bind mount (the admin integrity scan is non-recursive and ignores them).
    STAGING_DIR = os.path.join(PROJECT_ROOT, 'staging')
    STAGING_THUMB_DIR = os.path.join(THUMB_FOLDER, 'staging')

    # File-type allow-lists. HEIC was dropped: Pillow can't decode it without
    # pillow-heif, so uploads produced placeholder thumbs and browsers can't
    # display it inline anyway. AVI/MOV/MKV are accepted but may not play
    # inline in every browser.
    ALLOWED_IMAGE_EXT = {'jpg', 'jpeg', 'png', 'webp'}
    ALLOWED_GIF_EXT = {'gif'}
    ALLOWED_VIDEO_EXT = {'mp4', 'webm', 'avi', 'mov', 'mkv'}

    # Behaviour
    THUMB_SIZE = (150, 150)
    PAGE_SIZE = 30
    PORT = int(os.environ.get('PORT', 5001))
    BAN_THRESHOLD = 10
    # Automatic (threshold) bans lift after BAN_DURATION; the failed-login
    # counter restarts once attempts are more than FAIL_WINDOW apart.
    # Manual admin bans never expire.
    BAN_DURATION = timedelta(hours=float(os.environ.get('BAN_DURATION_HOURS', 24)))
    FAIL_WINDOW = timedelta(minutes=15)
    # The /register page. Closed by default: admins create accounts on the
    # admin panel. Open it once to create the first account, which becomes
    # an admin, then close it again.
    REGISTRATION_OPEN = os.environ.get('REGISTRATION_OPEN', 'false').strip().lower() == 'true'

    # Near-duplicate scan: hard wall-clock limit for fingerprinting one
    # video. ffmpeg can block indefinitely on a malformed file, and the
    # scan must never be wedged by a single item, so the ffmpeg process is
    # killed at this deadline and the item recorded as an error. Generous
    # by default — a long 4K clip is slow, not stuck.
    DUPE_ITEM_TIMEOUT = float(os.environ.get('DUPE_ITEM_TIMEOUT', 180))

    # Same hard wall-clock limit for rendering one video *thumbnail*, which
    # runs the same untrustworthy ffmpeg over the same outside-supplied
    # files. Both callers are single shared daemon threads — the upload
    # thumb worker and the WebDAV staging scanner — so an unbounded decode
    # there stops thumbnails, or the entire staging queue, for every user
    # until the container restarts.
    VIDEO_THUMB_TIMEOUT = float(os.environ.get('VIDEO_THUMB_TIMEOUT', 180))

    # Tags hidden by the "Hide AI" toggle (gallery, API, and feed use the same
    # list). Comma-separated; unset or empty keeps the default list.
    AI_TAGS = tuple(
        t.strip() for t in os.environ.get('AI_TAGS', '').split(',') if t.strip()
    ) or ('fake', 'ai', 'grok', 'deepfake', 'gemini', 'chatgpt')

    # Accounts allowed to attribute an upload to another user via the
    # 'uploaded_for' form field (the Discord bot maps Discord authors to
    # index users this way). Comma-separated usernames; defaults to the
    # bot's default account name.
    DELEGATED_UPLOADERS = frozenset(
        u.strip()
        for u in os.environ.get('DELEGATED_UPLOADERS', 'Bot').split(',')
        if u.strip()
    )

    # Telegram ingest bot's @handle, shown on /settings so a user knows which
    # bot to message. Display only — the bot's own token decides what it
    # actually is, and nothing here is used to authenticate anything.
    TELEGRAM_BOT_NAME = os.environ.get('TELEGRAM_BOT_NAME', '').strip().lstrip('@')

    # Flask
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024 * 1024  # 10 GiB

    # WebDAV inbox behaviour
    DAV_ENABLED = os.environ.get('DAV_ENABLED', 'true').lower() == 'true'
    STAGING_SCAN_INTERVAL = float(os.environ.get('STAGING_SCAN_INTERVAL', 5))
    # A staged file is processed once its size/mtime are unchanged for one
    # scan cycle, or immediately when its mtime is older than this.
    STAGING_STABLE_AGE = float(os.environ.get('STAGING_STABLE_AGE', 10))
    STAGING_SCANNER_ENABLED = os.environ.get('STAGING_SCANNER', 'true').lower() != 'false'

    # Sourced from environment
    SECRET_KEY = os.environ.get('SECRET_KEY')
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
    DEV_MODE = os.environ.get('DEV_MODE', '').lower() == 'true'

    # Peers whose X-Forwarded-* headers are honoured (comma-separated IPs or
    # CIDRs). Everything else keeps its real REMOTE_ADDR and has those
    # headers stripped — see app/proxy.py. Empty uses the loopback + private
    # ranges default, which covers the reverse-proxy container without
    # pinning an IP that changes on every recreate. Narrow it to the
    # proxy's exact address for a stronger guarantee.
    TRUSTED_PROXIES = os.environ.get('TRUSTED_PROXIES', '')

    # Cookie hardening — secure cookies off only in DEV_MODE
    SESSION_COOKIE_SECURE = not DEV_MODE
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # How long a login survives. login() marks the session permanent, which
    # does two things: the cookie gets a Max-Age so it outlives a browser or
    # PC restart instead of dying as a session cookie, and Flask starts
    # *enforcing* the timestamp it already signs into the value. Without
    # `permanent` that timestamp is deserialised with max_age=None, so a
    # captured cookie stayed valid forever server-side —
    # bounding it here is what makes the longer client-side life safe.
    #
    # SESSION_REFRESH_EACH_REQUEST is Flask's default True, so the window
    # rolls: an active user is never logged out mid-use, and the clock only
    # runs down over a genuine 30 days of not visiting.
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.environ.get('SESSION_DAYS', 30)))

    @staticmethod
    def tmp_dir():
        """Scratch directory for transient plaintext produced while encrypting
        or decrypting private media (whole-file video decrypts for thumbnail
        rendering, re-encryption during owner reassignment).

        Derived from UPLOAD_FOLDER at call time (not frozen at import) so it
        follows the folder when tests monkeypatch UPLOAD_FOLDER. It is a
        *subdirectory* of the upload volume, so large decrypts stay on the
        media disk, yet it is unreachable through the basename-only
        /media/<fname> and /thumbs/<fname> routes and is skipped by the
        integrity scan (which lists files, not directories). Created on demand.
        """
        d = os.path.join(Config.UPLOAD_FOLDER, '.tmp')
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def validate():
        if not Config.SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY environment variable is not set. "
                "Copy .env.example to .env and generate a strong random value."
            )
        if not Config.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set. "
                "The app requires PostgreSQL, e.g. "
                "postgresql://user:password@postgres:5432/mediaindex"
            )
