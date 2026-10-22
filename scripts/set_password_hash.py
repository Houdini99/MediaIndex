"""Write an already-made password hash directly to an existing user.

Unlike set_password.py (which takes a plaintext and hashes it), this takes
a hash you generated elsewhere and stores it verbatim.

The hash is read from an interactive prompt, a pipe, or a file — never from
a command-line argument. That is deliberate: a Werkzeug hash contains `$`
(and the matching password often contains `!`), both of which the shell
expands inside double quotes. Passing the hash on the command line is what
silently corrupted it before (`...8:1$salt$...` became `...8:1...`, salt
gone). Reading it via stdin/prompt keeps the shell out of the way, and the
structure is validated before any write so a mangled paste is rejected
instead of stored.

All database access goes through app.auth.db, so the script talks to the
same PostgreSQL database the app does (DATABASE_URL). The host cannot
reach it — it lives on the stack's internal network — so run the script
inside the web container:

    docker exec -i mediaindex-web python scripts/set_password_hash.py alice < hash.txt

Usage:
    python scripts/set_password_hash.py <username>            # prompts for the hash
    python scripts/set_password_hash.py <username> < hash.txt # read from a file
    some-generator | python scripts/set_password_hash.py <username>
    python scripts/set_password_hash.py <username> --hash-file hash.txt

Example:
    python scripts/set_password_hash.py alice
"""
import argparse
import sys

import _bootstrap  # sys.path + SECRET_KEY; must precede app.*

from app.auth.db import fetch_user_for_login, update_user_password
from app.security_log import log_event


# ── Hash validation ────────────────────────────────────────────────────────
def looks_like_hash(h: str):
    """Return (ok, reason). Catches the shell-mangling that drops the salt."""
    if h.count('$') < 2:
        return False, ("expected 'method$salt$hash' but found no '$' separators — "
                       "the shell likely ate part of it (double quotes expand $).")
    method, salt, digest = h.split('$', 2)
    scheme = method.split(':', 1)[0]
    # The only schemes Werkzeug's check_password_hash accepts; anything else
    # stored here would make that user's every login attempt fail.
    if scheme not in {'scrypt', 'pbkdf2'}:
        return False, f"unrecognized hash scheme {scheme!r}; expected 'scrypt' or 'pbkdf2'."
    if not salt:
        return False, "salt segment is empty — the hash looks truncated."
    if not digest:
        return False, "hash segment is empty."
    return True, ''


# ── Hash input (prompt / pipe / file) ─────────────────────────────────────
def read_hash(hash_file: str | None) -> str:
    if hash_file:
        with open(hash_file, 'r') as f:
            return f.read().strip()
    if not sys.stdin.isatty():            # piped or redirected input
        return sys.stdin.read().strip()
    return input('Paste password hash: ').strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write an already-made password hash to an existing user.")
    parser.add_argument('username', help='Username whose hash should be replaced.')
    parser.add_argument('--hash-file', help='Read the hash from this file instead of prompting.')
    args = parser.parse_args()

    _bootstrap.require_db(tty=False)
    row = fetch_user_for_login(args.username)
    if row is None:
        print(f"No such user: {args.username!r}", file=sys.stderr)
        return 1

    pw_hash = read_hash(args.hash_file)
    if not pw_hash:
        print('No hash provided.', file=sys.stderr)
        return 1

    ok, reason = looks_like_hash(pw_hash)
    if not ok:
        print(f'Refusing to store: {reason}', file=sys.stderr)
        print(f'Got: {pw_hash!r}', file=sys.stderr)
        return 1

    update_user_password(row[0], pw_hash)
    log_event('password_reset', user=args.username, by='command line')

    # Read it back and confirm it was stored byte-for-byte.
    if fetch_user_for_login(args.username)[1] == pw_hash:
        print(f"Hash stored for {args.username!r}.")
        return 0

    print('ERROR: stored value does not match the hash provided.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
