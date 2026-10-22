"""Import-for-side-effects preamble shared by the CLI scripts.

Every script in this directory needs the same two things to happen *before*
it can import `app.*`:

  1. the project root on sys.path, so `import app` resolves when the script
     is run as `python scripts/foo.py` from anywhere;
  2. a SECRET_KEY in the environment, so importing `app.config` succeeds.
     The value is never used — `Config.validate()` is not called on the CLI
     path, and nothing here signs a session — it only has to be non-empty.

Every script that touches the database also calls require_db() before
its first query, so a run on the host stops with a hint instead of a
connection error. The import line still carries a comment in every
script: it has to stay above the `app.*` imports, and moving it below
them fails at run time, with `ModuleNotFoundError: No module named 'app'`,
not at lint time.
"""
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SECRET_KEY', 'cli')


def require_db(tty=True):
    """Exit with a hint unless DATABASE_URL is set. The host has no route
    to the database, so the hint is the same command run inside the web
    container. tty=False for a script that may read a pipe, which
    `docker exec -t` refuses."""
    from app.config import Config
    if not Config.DATABASE_URL:
        cmd = shlex.join([os.path.basename(sys.argv[0]), *sys.argv[1:]])
        sys.exit('DATABASE_URL is not set — run inside the web container:\n'
                 f'  docker exec {"-it" if tty else "-i"} mediaindex-web python scripts/{cmd}')
