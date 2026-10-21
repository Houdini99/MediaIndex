"""Staging scanner: registration, stabilization, dedup, reconciliation.

scan_once() is called directly — the scanner thread never runs in tests.
"""
import os

import pytest

from app.config import Config


@pytest.fixture()
def scan(app):
    from app.media.staging import scan_once
    return scan_once


@pytest.fixture()
def staged(user):
    """Write a file into the user's staging dir; returns its path."""
    uid = user[0]
    user_dir = os.path.join(Config.STAGING_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)

    def _write(name, data=b'file-bytes'):
        path = os.path.join(user_dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    return _write


def _rows(uid):
    from app.media.staging_db import list_staging
    return list_staging(uid)


def test_new_file_registers_pending(scan, staged, user):
    staged('a.jpg')
    scan()
    rows = _rows(user[0])
    assert len(rows) == 1
    assert rows[0]['status'] == 'pending'
    assert rows[0]['fname'] == 'a.jpg'


def test_stable_file_becomes_ready(scan, staged, user):
    staged('a.jpg')
    scan()
    scan()  # unchanged across two cycles → processed
    row = _rows(user[0])[0]
    assert row['status'] == 'ready'
    assert row['filehash']
    assert row['ftype'] == 'image'
    thumb = os.path.join(Config.STAGING_THUMB_DIR, row['thumb'])
    assert os.path.isfile(thumb)


def test_growing_file_stays_pending(scan, staged, user):
    path = staged('a.jpg')
    scan()
    with open(path, 'ab') as f:
        f.write(b'more')
    scan()
    assert _rows(user[0])[0]['status'] == 'pending'


def test_known_hash_flagged_duplicate(scan, staged, user):
    from app.media.db import insert_media
    from app.media.processing import sha256_file
    path = staged('dup.jpg')
    fh = sha256_file(path)
    insert_media('x.jpg', 'x.png', 'tag', fh, user[0])
    scan()
    scan()
    assert _rows(user[0])[0]['status'] == 'duplicate'


def test_unsupported_extension(scan, staged, user):
    staged('notes.txt')
    scan()
    scan()
    assert _rows(user[0])[0]['status'] == 'unsupported'


def test_deleted_file_reconciled(scan, staged, user):
    path = staged('a.jpg')
    scan()
    scan()
    row = _rows(user[0])[0]
    thumb = os.path.join(Config.STAGING_THUMB_DIR, row['thumb'])
    os.remove(path)
    scan()
    assert _rows(user[0]) == []
    assert not os.path.exists(thumb)


def test_overwritten_ready_file_reprocessed(scan, staged, user):
    path = staged('a.jpg')
    scan()
    scan()
    assert _rows(user[0])[0]['status'] == 'ready'
    with open(path, 'ab') as f:
        f.write(b'overwritten-with-more-bytes')
    scan()
    assert _rows(user[0])[0]['status'] == 'pending'


def test_junk_files_skipped(scan, staged, user):
    staged('.DS_Store')
    staged('Thumbs.db')
    staged('download.jpg.part')
    staged('._resource-fork.jpg')
    scan()
    assert _rows(user[0]) == []


def test_processing_failure_marks_error_once(scan, staged, user, monkeypatch):
    """A file that fails processing must end up 'error' (not retried every
    cycle), and a DAV re-upload must send it back through the pipeline."""
    from app.media.processing import sha256_file as real_sha256
    path = staged('bad.jpg')
    failing = {'on': True}

    def flaky(p):
        if failing['on']:
            raise OSError('unreadable')
        return real_sha256(p)

    # NOTE: never monkeypatch.undo() here — it would also undo conftest's
    # Config path patches and point the scanner at the real project dirs.
    monkeypatch.setattr('app.media.staging.sha256_file', flaky)
    scan()
    scan()
    row = _rows(user[0])[0]
    assert row['status'] == 'error'
    # Further scans leave it alone (no infinite retry).
    scan()
    assert _rows(user[0])[0]['status'] == 'error'
    # Re-upload (content change) resets it to pending, then processes fine.
    failing['on'] = False
    with open(path, 'ab') as f:
        f.write(b'new-content')
    scan()
    assert _rows(user[0])[0]['status'] == 'pending'
    scan()
    assert _rows(user[0])[0]['status'] == 'ready'


def test_size_update_never_clobbers_promoting(scan, staged, user):
    from app.media.staging_db import claim_for_promote, update_staging_size
    staged('a.jpg')
    scan()
    scan()
    sid = _rows(user[0])[0]['id']
    assert claim_for_promote(sid, user[0])
    update_staging_size(sid, 999, 999.0)
    assert _rows(user[0])[0]['status'] == 'promoting'


def test_processed_files_never_rehashed_or_rethumbed(scan, staged, user, monkeypatch):
    """Steady-state cost per already-processed file must be one os.stat —
    the hash and thumbnail are computed exactly once until the file changes."""
    from app.media.processing import gen_image_thumb as real_thumb
    from app.media.processing import sha256_file as real_sha256
    calls = {'sha256': 0, 'thumb': 0}

    def counting_sha256(p):
        calls['sha256'] += 1
        return real_sha256(p)

    def counting_thumb(src, dst):
        calls['thumb'] += 1
        return real_thumb(src, dst)

    monkeypatch.setattr('app.media.staging.sha256_file', counting_sha256)
    monkeypatch.setattr('app.media.staging.gen_image_thumb', counting_thumb)

    path = staged('a.jpg')
    staged('b.jpg', b'other-bytes')
    scan()
    scan()  # both files become ready here
    assert calls == {'sha256': 2, 'thumb': 2}

    for _ in range(10):
        scan()
    assert calls == {'sha256': 2, 'thumb': 2}, 'ready files were re-processed'

    # Only an actual content change triggers exactly one re-process.
    with open(path, 'ab') as f:
        f.write(b'changed')
    scan()   # detected -> pending
    scan()   # stable -> re-processed
    scan()
    scan()
    assert calls == {'sha256': 3, 'thumb': 3}


def test_promoting_rows_left_alone(scan, staged, user):
    from app.media.staging_db import claim_for_promote
    path = staged('a.jpg')
    scan()
    scan()
    sid = _rows(user[0])[0]['id']
    assert claim_for_promote(sid, user[0])
    # Even if the file vanishes, a fresh claim is not reconciled away.
    os.remove(path)
    scan()
    assert _rows(user[0])[0]['status'] == 'promoting'
