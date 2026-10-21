"""Shared fixtures: an app factory bound to a per-session Postgres test DB.

The suite requires a Postgres server, pointed at via TEST_DATABASE_URL:

    docker run --rm -d -p 127.0.0.1:5433:5432 \
        -e POSTGRES_PASSWORD=test postgres:17-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \
        pytest

A throwaway database (mediaindex_test_<pid>) is created on that server for
the session and dropped afterwards; tables are truncated between tests.
"""
import os
import sys
import tempfile

# Must be set before anything imports app.* (Config and the limiter read
# the environment at import time).
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('REDIS_URL', 'memory://')
os.environ.setdefault('DEV_MODE', 'true')
# The staging scanner thread must never run inside tests — scanner behavior
# is tested by calling scan_once() directly.
os.environ.setdefault('STAGING_SCANNER', 'false')
# Tests are isolated in their own throwaway database; a DATABASE_URL
# inherited from the shell must never leak into the app under test.
os.environ.pop('DATABASE_URL', None)

TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL', '')
if not TEST_DATABASE_URL:
    raise SystemExit(
        "TEST_DATABASE_URL is not set. The suite needs a Postgres server, e.g.\n"
        "  docker run --rm -d -p 127.0.0.1:5433:5432 -e POSTGRES_PASSWORD=test postgres:17-alpine\n"
        "  TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres pytest"
    )

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from werkzeug.security import generate_password_hash

from app.config import Config

# Nothing a test does may write into the real log/ folder: on a server that
# folder is the live stack's bind mount. The app fixture narrows this to a
# per-test directory; this covers tests that don't use it.
Config.LOG_FOLDER = tempfile.mkdtemp(prefix='mediaindex-test-log-')


# ── Offline stand-ins for the bot's dependencies ──────────────────────────
# discord_bot/ ships its own requirements (discord.py, aiohttp, yt-dlp) and
# is not installed alongside the app's, so its modules are faked here —
# once, in one place, so two test modules can't install conflicting
# versions of the same stub depending on collection order. Anything really
# installed always wins.
def _stub(name, build):
    import importlib
    try:
        importlib.import_module(name)
    except ImportError:
        sys.modules[name] = build()


def _build_aiohttp():
    import types
    mod = types.ModuleType('aiohttp')

    class FormData:
        def __init__(self):
            self.fields = []

        def add_field(self, name, value, **kw):
            self.fields.append((name, value, kw))

    class BasicAuth:
        def __init__(self, login, password=''):
            self.login, self.password = login, password

    mod.FormData = FormData
    mod.BasicAuth = BasicAuth
    mod.ClientTimeout = lambda **kw: kw
    mod.CookieJar = lambda **kw: kw
    mod.ClientSession = lambda **kw: kw
    mod.ClientError = type('ClientError', (Exception,), {})
    mod.ClientResponseError = type('ClientResponseError',
                                   (mod.ClientError,), {})
    return mod


def _build_discord():
    import types
    mod = types.ModuleType('discord')

    class Intents:
        def __init__(self):
            self.message_content = False

        @staticmethod
        def default():
            return Intents()

    class Client:
        def __init__(self, intents=None):
            self.intents = intents

    mod.Intents = Intents
    mod.Client = Client
    mod.HTTPException = type('HTTPException', (Exception,), {})
    mod.NotFound = type('NotFound', (mod.HTTPException,), {})
    mod.DMChannel = type('DMChannel', (), {})
    mod.Embed = type('Embed', (), {})
    mod.Colour = type('Colour', (), {})
    mod.File = type('File', (), {})
    return mod


def _build_telegram():
    """Minimal stand-in for python-telegram-bot.

    telegram_bot/ ships its own requirements and is not installed
    alongside the app's. Only what bot.py touches at *import* time is
    faked — the handler classes it registers and the filter names it
    composes — because the tests exercise the bot's own logic (the gate,
    album batching, tag routing), never the library's.
    """
    import types
    mod = types.ModuleType('telegram')
    error = types.ModuleType('telegram.error')
    ext = types.ModuleType('telegram.ext')
    filters_mod = types.ModuleType('telegram.ext.filters')

    mod.Update = type('Update', (), {'ALL_TYPES': ['message']})
    error.TelegramError = type('TelegramError', (Exception,), {})

    class _Filter:
        """Stands in for a filter expression; composes into itself."""
        def __or__(self, other):
            return self

        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    for _name in ('PHOTO', 'VIDEO', 'ANIMATION', 'TEXT', 'COMMAND'):
        setattr(filters_mod, _name, _Filter())
    filters_mod.Document = types.SimpleNamespace(ALL=_Filter())
    filters_mod.ChatType = types.SimpleNamespace(PRIVATE=_Filter())

    class _Recorder:
        def __init__(self, *a, **kw):
            self.args, self.kwargs = a, kw

    class _Application:
        def __init__(self):
            self.handlers = []
            self.error_handlers = []
            self.bot_data = {}

        def add_handler(self, handler):
            self.handlers.append(handler)

        def add_error_handler(self, handler):
            self.error_handlers.append(handler)

        def run_polling(self, **kw):
            raise AssertionError("tests must never start polling")

    class _Builder:
        def __init__(self):
            self.calls = []

        def _record(self, name, value):
            self.calls.append((name, value))
            return self

        def token(self, value):
            return self._record('token', value)

        def base_url(self, value):
            return self._record('base_url', value)

        def local_mode(self, value):
            return self._record('local_mode', value)

        def post_shutdown(self, value):
            return self._record('post_shutdown', value)

        def build(self):
            app = _Application()
            app.builder_calls = self.calls
            return app

    ext.ApplicationBuilder = _Builder
    ext.CommandHandler = _Recorder
    ext.MessageHandler = _Recorder
    ext.filters = filters_mod
    mod.error = error
    mod.ext = ext
    sys.modules['telegram.error'] = error
    sys.modules['telegram.ext'] = ext
    sys.modules['telegram.ext.filters'] = filters_mod
    return mod


_stub('aiohttp', _build_aiohttp)
_stub('discord', _build_discord)
_stub('telegram', _build_telegram)

# The bots' config modules read these at import time and fail fast
# without them.
for _var, _value in (('DISCORD_BOT_TOKEN', 'test-token'),
                     ('DISCORD_NOTIFY_CHANNEL_ID', '1'),
                     ('DISCORD_INGEST_CHANNEL_ID', '2'),
                     ('TELEGRAM_BOT_TOKEN', 'test-telegram-token'),
                     ('INDEX_BOT_APP_PASSWORD', 'test-app-password')):
    os.environ.setdefault(_var, _value)


# ── Per-session Postgres test database ─────────────────────────────────────
def _pg_url_for_db(base_url, dbname):
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(base_url)
    return urlunsplit(parts._replace(path=f'/{dbname}'))


@pytest.fixture(scope='session')
def _pg_test_db():
    """Create a throwaway database on the TEST_DATABASE_URL server for this
    session; yields its URL and drops it afterwards."""
    import psycopg
    dbname = f'mediaindex_test_{os.getpid()}'
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS {dbname}')
        conn.execute(f'CREATE DATABASE {dbname}')
    yield _pg_url_for_db(TEST_DATABASE_URL, dbname)
    # Close the app's pool before dropping (a live connection blocks DROP).
    import app.db_utils as db_utils
    if db_utils._pool is not None:
        db_utils._pool.close()
        db_utils._pool = None
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS {dbname}')


def _pg_reset_tables():
    """Empty every table between tests — schema stays, ids restart.

    The near-duplicate tables are listed explicitly rather than left to
    CASCADE, so a future FK change can't silently leave state behind
    between tests.
    """
    from app.db_utils import db_conn, ensure_pg_schema
    ensure_pg_schema()
    with db_conn() as conn:
        conn.cursor().execute(
            'TRUNCATE users.failed_logins, users.site_logins, media.staging, '
            'media.media_hash, media.media_hash_meta, media.dupe_dismissed, '
            'media.dupe_quarantine, media.dupe_job, media.media, '
            'users.users RESTART IDENTITY CASCADE'
        )
        conn.commit()


@pytest.fixture()
def app(tmp_path, monkeypatch, _pg_test_db):
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'media_files'))
    monkeypatch.setattr(Config, 'THUMB_FOLDER', str(tmp_path / 'thumbnails'))
    monkeypatch.setattr(Config, 'DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(Config, 'STAGING_DIR', str(tmp_path / 'staging'))
    monkeypatch.setattr(Config, 'STAGING_THUMB_DIR', str(tmp_path / 'thumbnails' / 'staging'))
    monkeypatch.setattr(Config, 'DATABASE_URL', _pg_test_db)
    monkeypatch.setattr(Config, 'LOG_FOLDER', str(tmp_path / 'log'))
    _pg_reset_tables()

    from app import create_app
    from app.extensions import limiter

    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.enabled = False
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def user(app):
    """A regular user; returns (id, username, password)."""
    from app.auth.db import insert_user, fetch_user_for_login, set_user_admin
    username, password = 'alice', 'password123'
    insert_user(username, generate_password_hash(password))
    uid = fetch_user_for_login(username)[0]
    # The first account ever created is bootstrapped as admin; these tests
    # want a plain user.
    set_user_admin(uid, False)
    return uid, username, password


@pytest.fixture()
def logged_in(client, user):
    uid, username, password = user
    resp = client.post('/login', data={'username': username, 'password': password})
    assert resp.status_code == 302
    return uid


@pytest.fixture()
def dav_user(app, user):
    """A user with a WebDAV app password; returns (id, username, token)."""
    from app.auth.db import hash_dav_password, set_dav_password_hash
    uid, username, _password = user
    token = 'test-dav-token-123'
    set_dav_password_hash(uid, hash_dav_password(token))
    return uid, username, token


# ── Video decoding stand-ins ──────────────────────────────────────────────
@pytest.fixture()
def hanging_ffmpeg(tmp_path, monkeypatch):
    """Point app.media.ffmpeg at an ffmpeg/ffprobe that never returns — the
    malformed-file behaviour every video caller must survive."""
    from app.media import ffmpeg
    stub = tmp_path / 'hanging-ffmpeg'
    stub.write_text('#!/bin/sh\nexec sleep 300\n')
    stub.chmod(0o755)
    monkeypatch.setattr(ffmpeg, 'FFMPEG', str(stub))
    monkeypatch.setattr(ffmpeg, 'FFPROBE', str(stub))
    return stub


@pytest.fixture(scope='session')
def sample_video(tmp_path_factory):
    """A real 4-second 320x240 H.264 clip, made with the system ffmpeg.
    Skips where ffmpeg is not installed (the web image always has it)."""
    import shutil
    import subprocess
    if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
        pytest.skip('ffmpeg/ffprobe not installed')
    path = tmp_path_factory.mktemp('video') / 'sample.mp4'
    subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'lavfi',
         '-i', 'testsrc=duration=4:size=320x240:rate=25',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)],
        check=True, timeout=60)
    return str(path)
