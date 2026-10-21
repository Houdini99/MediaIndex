"""staging_routes._own_staging_path — the second per-user path jail.

`test_staging_promote.py` already covers the promote/reject flow end to
end, including the 409 and 410 paths and row-level ownership. What it does
not cover is the containment check itself: every disk path derived from a
staging row is re-validated to sit inside the requesting user's own
directory before the file is served, moved, or deleted.

That guard matters because the three operations behind it are `send_file`,
`shutil.move`, and `os.remove`. Row ownership alone does not constrain
where `row['fname']` points — the scanner builds it with `os.path.relpath`
while walking the user's own directory, but a symlink inside that
directory, or any future writer of the column, could produce a name that
resolves elsewhere. This is the staging-side analogue of the WebDAV chroot
in `test_dav_provider.py`, and like that one it fails closed.
"""
import os

import pytest
from flask import g

from app.config import Config
from app.media import staging_db
from app.media.staging_routes import _own_staging_path


@pytest.fixture()
def jails(app, logged_in):
    """The logged-in user's staging dir, plus a neighbour's holding a file."""
    mine = os.path.join(Config.STAGING_DIR, str(logged_in))
    theirs = os.path.join(Config.STAGING_DIR, str(logged_in + 1))
    os.makedirs(mine, exist_ok=True)
    os.makedirs(theirs, exist_ok=True)
    with open(os.path.join(theirs, 'secret.jpg'), 'wb') as fh:
        fh.write(b'not yours')
    return logged_in, mine, theirs


def resolve(client, uid, fname):
    """Call the guard inside a request context, as a route would."""
    with client.application.test_request_context():
        g.user = {'id': uid, 'username': 'tester', 'is_admin': False}
        return _own_staging_path({'fname': fname})


# ── Allowed ────────────────────────────────────────────────────────────────
def test_own_file_resolves(client, jails):
    uid, mine, _ = jails
    got = resolve(client, uid, 'photo.jpg')
    assert os.path.realpath(got) == os.path.realpath(os.path.join(mine, 'photo.jpg'))


def test_nested_own_file_resolves(client, jails):
    uid, mine, _ = jails
    got = resolve(client, uid, 'sub/dir/photo.jpg')
    assert os.path.realpath(got).startswith(os.path.realpath(mine) + os.sep)


# ── Refused ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('fname', [
    '../secret.jpg',              # up into the staging root
    '../../etc/passwd',           # out of staging entirely
    'sub/../../etc/passwd',       # out via a nested path
    '/etc/passwd',                # absolute
])
def test_escapes_are_refused(client, jails, fname):
    uid, _, _ = jails
    from werkzeug.exceptions import NotFound
    with pytest.raises(NotFound):
        resolve(client, uid, fname)


def test_crossing_into_another_users_jail_is_refused(client, jails):
    """Row ownership is checked separately; this is the belt to that
    braces — a row that names a neighbour's file still resolves nowhere."""
    uid, _, _ = jails
    from werkzeug.exceptions import NotFound
    with pytest.raises(NotFound):
        resolve(client, uid, f'../{uid + 1}/secret.jpg')


def test_symlink_out_of_the_jail_is_refused(client, jails, tmp_path):
    """realpath() on both sides is what makes this work: a symlink looks
    contained as a string and is not."""
    uid, mine, _ = jails
    outside = tmp_path / 'outside'
    outside.mkdir(exist_ok=True)
    (outside / 'loot.jpg').write_bytes(b'secret')
    link = os.path.join(mine, 'escape')
    if os.path.islink(link) or os.path.exists(link):
        os.unlink(link)
    os.symlink(str(outside), link)

    from werkzeug.exceptions import NotFound
    with pytest.raises(NotFound):
        resolve(client, uid, 'escape/loot.jpg')


def test_a_user_id_prefix_is_not_a_path_prefix(client, app, logged_in):
    """commonpath() compares components, so user 1 must not reach the
    directory of user 11 by string prefix."""
    os.makedirs(os.path.join(Config.STAGING_DIR, '1'), exist_ok=True)
    os.makedirs(os.path.join(Config.STAGING_DIR, '11'), exist_ok=True)
    from werkzeug.exceptions import NotFound
    with pytest.raises(NotFound):
        resolve(client, 1, '../11/secret.jpg')


# ── The guard is actually wired into the routes ────────────────────────────
def test_routes_go_through_the_guard(client, logged_in):
    """A row whose fname escapes must 404 through the real endpoints, not
    just when the helper is called directly."""
    user_dir = os.path.join(Config.STAGING_DIR, str(logged_in))
    os.makedirs(user_dir, exist_ok=True)
    sid = staging_db.insert_staging_row(logged_in, '../../etc/passwd', 10, 1.0)
    assert sid, "could not stage a row to test the guard with"
    staging_db.update_staging_processed(sid, 'deadbeef', None, 'image', 'ready')

    assert client.get(f'/staging/file/{sid}').status_code == 404
    assert client.post(f'/api/staging/{sid}/reject').status_code == 404
