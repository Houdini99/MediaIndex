"""Import-for-side-effects preamble shared by the CLI scripts.

Every script in this directory needs the same two things to happen *before*
it can import `app.*`:

  1. the project root on sys.path, so `import app` resolves when the script
     is run as `python scripts/foo.py` from anywhere;
  2. a SECRET_KEY in the environment, so importing `app.config` succeeds.
     The value is never used — `Config.validate()` is not called on the CLI
     path, and nothing here signs a session — it only has to be non-empty.

This module is imported purely for those side effects and exports nothing.
That makes it look deletable to both a human skimming the imports and to
any unused-import tool, so the import line carries a `noqa` and a comment
in every script, matching how the blueprint packages mark their own
side-effect imports (`from . import routes  # noqa: E402,F401`).

Removing the import from a script does not fail at lint time. It fails at
run time, with `ModuleNotFoundError: No module named 'app'`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SECRET_KEY', 'cli')
