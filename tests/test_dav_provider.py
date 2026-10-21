"""app/dav/provider.py — the per-user WebDAV jail.

This is the boundary that stops WebDAV user A from reading or writing user
B's staged files, and it was the only security boundary in the codebase
with no direct test. `test_dav_auth.py` covers `dc.py` —
who may connect — but nothing covered *what they can reach once connected*.

It is also the boundary where a regression fails **open** rather than
loudly: `_loc_to_file_path` is an override of a wsgidav internal, so an
upstream rename or signature change would silently restore the base class's
behaviour, which resolves paths against the shared staging root with no
per-user prefix at all. Two tests below pin the override itself for that
reason.

Every path assertion is made against `os.path.realpath`, matching the
containment check, so a symlink cannot smuggle a path past a string
comparison that the filesystem would resolve differently.
"""
import os

import pytest
from wsgidav.dav_error import HTTP_FORBIDDEN, DAVError
from wsgidav.fs_dav_provider import FilesystemProvider

from app.dav.dc import ENVIRON_USER_ID
from app.dav.provider import UserStagingProvider


@pytest.fixture()
def staging(tmp_path):
    """A staging root with two users' jails, each holding one file."""
    for uid in (1, 2):
        d = tmp_path / str(uid)
        d.mkdir()
        (d / 'own.txt').write_text(f'user {uid}')
    (tmp_path / 'shared-secret.txt').write_text('not inside any jail')
    return tmp_path


@pytest.fixture()
def provider(staging):
    return UserStagingProvider(str(staging))


def env(uid):
    return {ENVIRON_USER_ID: uid}


def resolve(provider, path, uid=1):
    return provider._loc_to_file_path(path, env(uid))


# ── The happy path ─────────────────────────────────────────────────────────
def test_resolves_inside_the_users_own_jail(provider, staging):
    got = resolve(provider, '/own.txt', uid=1)
    assert os.path.realpath(got) == os.path.realpath(staging / '1' / 'own.txt')


def test_root_resolves_to_the_users_jail_not_the_staging_root(provider, staging):
    """The prefix is the whole point: '/' must mean staging/<uid>/, never
    staging/ — otherwise every user browses every other user's folder."""
    got = resolve(provider, '/', uid=2)
    assert os.path.realpath(got) == os.path.realpath(staging / '2')
    assert os.path.realpath(got) != os.path.realpath(staging)


def test_each_user_gets_a_different_jail(provider):
    assert resolve(provider, '/own.txt', uid=1) != resolve(provider, '/own.txt', uid=2)


def test_nested_paths_stay_inside(provider, staging):
    got = resolve(provider, '/sub/dir/file.txt', uid=1)
    assert os.path.realpath(got).startswith(os.path.realpath(staging / '1') + os.sep)


# ── Escapes ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('path', [
    '/../shared-secret.txt',          # up into the staging root
    '/../../etc/passwd',              # up out of staging entirely
    '/../2/own.txt',                  # sideways into another user's jail
    '/sub/../../2/own.txt',           # sideways via a nested path
    '/../../../../../../etc/passwd',  # far past the filesystem root
    '/..',                            # the parent directory itself
])
def test_traversal_is_refused(provider, path):
    with pytest.raises(DAVError) as exc:
        resolve(provider, path, uid=1)
    assert exc.value.value == HTTP_FORBIDDEN


def test_absolute_path_cannot_escape(provider, staging):
    """A leading slash is stripped and the rest joined onto the jail, so an
    absolute-looking request stays contained rather than being honoured."""
    got = resolve(provider, '/etc/passwd', uid=1)
    assert os.path.realpath(got) == os.path.realpath(staging / '1' / 'etc' / 'passwd')


def test_symlink_out_of_the_jail_is_refused(provider, staging, tmp_path):
    """The containment check realpath()s both sides precisely so a symlink
    cannot point outside while still looking contained as a string."""
    outside = tmp_path.parent / 'outside-target'
    outside.mkdir(exist_ok=True)
    (outside / 'loot.txt').write_text('secret')
    link = staging / '1' / 'escape'
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(str(outside), str(link))

    with pytest.raises(DAVError) as exc:
        resolve(provider, '/escape/loot.txt', uid=1)
    assert exc.value.value == HTTP_FORBIDDEN


def test_symlink_into_another_users_jail_is_refused(provider, staging):
    link = staging / '1' / 'peek'
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(str(staging / '2'), str(link))

    with pytest.raises(DAVError) as exc:
        resolve(provider, '/peek/own.txt', uid=1)
    assert exc.value.value == HTTP_FORBIDDEN


def test_a_user_id_prefix_is_not_a_path_prefix(provider, staging):
    """uid 1 and uid 11 share a string prefix; containment must compare
    path components, not characters, or user 1 could reach staging/11/."""
    (staging / '11').mkdir(exist_ok=True)
    (staging / '11' / 'own.txt').write_text('user 11')
    with pytest.raises(DAVError):
        resolve(provider, '/../11/own.txt', uid=1)
    assert os.path.realpath(resolve(provider, '/own.txt', uid=11)) == \
        os.path.realpath(staging / '11' / 'own.txt')


# ── Unauthenticated / misconfigured ────────────────────────────────────────
@pytest.mark.parametrize('environ', [None, {}, {'other': 'value'}])
def test_missing_user_id_is_refused(provider, environ):
    """Fails closed. Without the uid there is no jail to resolve against,
    and the base class would fall back to the shared staging root."""
    with pytest.raises(DAVError) as exc:
        provider._loc_to_file_path('/own.txt', environ)
    assert exc.value.value == HTTP_FORBIDDEN


# ── The override itself ────────────────────────────────────────────────────
def test_override_actually_replaces_the_base_implementation():
    """If upstream renames this hook, the override silently stops running
    and every user lands in the shared staging root. Pin the wiring."""
    assert UserStagingProvider._loc_to_file_path is not \
        FilesystemProvider._loc_to_file_path
    assert hasattr(FilesystemProvider, '_loc_to_file_path'), \
        "wsgidav renamed the hook UserStagingProvider overrides"


def test_base_class_would_not_isolate_users(staging):
    """Demonstrates what the override buys: the stock provider resolves
    both users to the same file, with no per-user prefix at all."""
    base = FilesystemProvider(str(staging), fs_opts={})
    assert base._loc_to_file_path('/own.txt', env(1)) == \
        base._loc_to_file_path('/own.txt', env(2))
