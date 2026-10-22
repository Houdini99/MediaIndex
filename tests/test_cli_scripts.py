"""The command-line side of scripts/: tags.py and the shared DATABASE_URL
guard in _bootstrap.py.

What the scripts call into (rename_tag, remove_tag, normalize_tags, the
password helpers) is tested on its own; this covers the argument parsing,
the printed report and the exits an admin sees.
"""
import os
import sys

import pytest

from app.media.db import get_media_by_hash, get_media_by_id, insert_media
from app.media.tagging import tag_list

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'scripts')


@pytest.fixture()
def run(monkeypatch, capsys):
    """Run a script's main() with `argv`; returns (exit code, stdout, stderr)."""
    monkeypatch.syspath_prepend(SCRIPTS)

    def _run(script, *argv):
        module = __import__(script)
        monkeypatch.setattr(sys, 'argv', [f'scripts/{script}.py', *argv])
        try:
            code = module.main() or 0
        except SystemExit as e:
            code = e.code
        out, err = capsys.readouterr()
        return code, out, err
    return _run


@pytest.fixture()
def uid(app, user):
    return user[0]


def _mk(uid, tags, n=0):
    insert_media(f'f{n}.jpg', f'f{n}.png', tags, f'hash{n}', uid)
    return get_media_by_hash(f'hash{n}')[0]


def _tags_of(mid):
    return tag_list(get_media_by_id(mid)['tags'])


def test_rename_dry_run_reports_and_writes_nothing(uid, run):
    mid = _mk(uid, 'blackandwhite,beach')

    code, out, _ = run('tags', 'rename', 'blackandwhite', 'black_and_white', '--dry-run')

    assert code == 0
    assert f'#{mid}: blackandwhite,beach' in out
    assert 'would update 1 row(s).' in out
    assert 'blackandwhite' in _tags_of(mid)


def test_rename_writes(uid, run):
    mid = _mk(uid, 'blackandwhite')

    code, out, _ = run('tags', 'rename', 'blackandwhite', 'black_and_white')

    assert code == 0 and 'updated 1 row(s).' in out
    assert 'black_and_white' in _tags_of(mid)


def test_rename_to_the_same_tag_is_refused(app, run):
    code, _, _ = run('tags', 'rename', 'beach', 'beach')

    assert code == 'old and new tag are identical; nothing to do.'


def test_remove_reports_an_emptied_row(uid, run):
    from app.db_utils import db_conn
    mid = _mk(uid, 'blurry')
    with db_conn() as conn:   # without insert_media's user: tag
        conn.execute("UPDATE media.media SET tags = 'blurry' WHERE id = %s", (mid,))

    code, out, _ = run('tags', 'remove', 'blurry')

    assert code == 0
    assert f'#{mid}: blurry  ->  (empty)' in out and 'updated 1 row(s).' in out


def test_normalize_quotes_both_sides(uid, run):
    from app.db_utils import db_conn
    mid = _mk(uid, 'public')
    with db_conn() as conn:
        conn.execute("UPDATE media.media SET tags = 'public, beach' WHERE id = %s", (mid,))

    code, out, _ = run('tags', 'normalize')

    assert code == 0
    assert f"#{mid}: 'public, beach'  ->  'public,beach'" in out


def test_a_subcommand_is_required(run):
    code, _, err = run('tags')

    assert code == 2 and 'required' in err


@pytest.mark.parametrize('script, argv, hint', [
    ('tags', ['remove', 'blurry'],
     'docker exec -it mediaindex-web python scripts/tags.py remove blurry'),
    ('set_password_hash', ['alice'],
     'docker exec -i mediaindex-web python scripts/set_password_hash.py alice'),
])
def test_without_a_database_the_hint_repeats_the_command(monkeypatch, run, script, argv, hint):
    from app.config import Config
    monkeypatch.setattr(Config, 'DATABASE_URL', '')

    code, _, _ = run(script, *argv)

    assert code.startswith('DATABASE_URL is not set')
    assert code.endswith(hint)
