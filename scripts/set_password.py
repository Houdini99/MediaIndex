"""Set (replace) an existing user's password.

Prompts for the password via getpass so the plaintext and the resulting
hash never pass through the shell — this avoids the quoting/expansion
footguns (bash eating `$salt` or history-expanding `!`) that corrupt a
hash when it's pasted into a raw SQL command.

All database access goes through app.auth.db, so the script talks to the
same PostgreSQL database the app does (DATABASE_URL). The host cannot
reach it — it lives on the stack's internal network — so run the script
inside the web container:

    docker exec -it mediaindex-web python scripts/set_password.py alice

Usage:
    python scripts/set_password.py <username>
"""
import argparse
import getpass
import sys

import _bootstrap  # sys.path + SECRET_KEY; must precede app.*

from werkzeug.security import check_password_hash, generate_password_hash

from app.auth.db import fetch_user_for_login, update_user_password
from app.auth.routes import MIN_PASSWORD_LENGTH
from app.security_log import log_event


def prompt_password() -> str:
    while True:
        pw = getpass.getpass('New password: ')
        if len(pw) < MIN_PASSWORD_LENGTH:
            print(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', file=sys.stderr)
            continue
        if pw != getpass.getpass('Confirm password: '):
            print('Passwords do not match. Try again.', file=sys.stderr)
            continue
        return pw


def main() -> int:
    parser = argparse.ArgumentParser(description="Replace an existing user's password.")
    parser.add_argument('username', help='Username whose password should be replaced.')
    args = parser.parse_args()

    _bootstrap.require_db()
    row = fetch_user_for_login(args.username)
    if row is None:
        print(f"No such user: {args.username!r}", file=sys.stderr)
        return 1

    password = prompt_password()
    update_user_password(row[0], generate_password_hash(password))
    log_event('password_reset', user=args.username, by='command line')

    # Read the value back and verify the round-trip, so a success message
    # actually means login will work.
    if check_password_hash(fetch_user_for_login(args.username)[1], password):
        print(f"Password updated for {args.username!r}. Verified OK.")
        return 0

    print('ERROR: stored hash failed verification — password NOT reliably set.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
