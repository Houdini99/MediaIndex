"""Concurrent ingest of identical bytes must not destroy an existing file.

Stored names are content-addressed, so two uploads of the same bytes agree
on the same path *before* either has a database row. The old pipeline wrote
the plaintext `<hash>.<ext>`, encrypted it, then deleted it — which, if a
public upload of those bytes had already claimed that name and committed a
row, removed the file out from under it. The row survived pointing at
nothing: silent data loss, surfaced only by the integrity scan.

The fix is structural rather than a lock: a private upload encrypts
straight to `<hash>.<ext>.enc` and never creates or deletes the plaintext
name, so the two privacy variants cannot collide at all. These tests pin
that property, which is why they assert on *paths* and not on timing —
a lock-based fix would still pass a timing test on a lucky interleave.
"""
import io
import os
import threading

from PIL import Image
from werkzeug.security import generate_password_hash

from app.auth.db import insert_user
from app.config import Config
from app.media.ingest import ingest_file


def _png_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (24, 24), 'teal').save(buf, format='PNG')
    return buf.getvalue()


def _temp_upload(data, ext='png'):
    """Deliver bytes into UPLOAD_FOLDER the way a request would."""
    import tempfile
    fd, path = tempfile.mkstemp(dir=Config.UPLOAD_FOLDER, suffix=f'.{ext}')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
    return path


def _user(name):
    insert_user(name, generate_password_hash('password123'))
    from app.media.db import get_user_id_by_username
    return get_user_id_by_username(name)


def test_private_ingest_never_creates_the_plaintext_name(app):
    """The property the fix rests on. If `<hash>.<ext>` is never created,
    it can never be deleted out from under a public row."""
    uid = _user('racer1')
    data = _png_bytes()
    status, fh = ingest_file(_temp_upload(data), 'png', 'racetest', uid,
                             is_private=True)
    assert status == 'ok'

    plain = os.path.join(Config.UPLOAD_FOLDER, f'{fh}.png')
    plain_thumb = os.path.join(Config.THUMB_FOLDER, f'{fh}.png')
    assert not os.path.exists(plain), "plaintext media name was created"
    assert not os.path.exists(plain_thumb), "plaintext thumb name was created"
    assert os.path.exists(os.path.join(Config.UPLOAD_FOLDER, f'{fh}.png.enc'))
    assert os.path.exists(os.path.join(Config.THUMB_FOLDER, f'{fh}.png.enc'))


def test_private_ingest_leaves_no_plaintext_in_the_scratch_dir(app):
    """The rendered thumbnail passes through Config.tmp_dir(); it must not
    be left behind, since that would be an unencrypted copy of a private
    image sitting on the media volume."""
    uid = _user('racer2')
    ingest_file(_temp_upload(_png_bytes()), 'png', 'racetest', uid,
                is_private=True)
    leftovers = [n for n in os.listdir(Config.tmp_dir()) if n.startswith('ingest-')]
    assert leftovers == [], f"scratch plaintext left behind: {leftovers}"


def test_public_then_private_keeps_the_public_file(app):
    """The original bug, in sequence: the public row's file must survive a
    later private ingest of the very same bytes."""
    pub, priv = _user('racer3'), _user('racer4')
    data = _png_bytes()

    status, fh = ingest_file(_temp_upload(data), 'png', 'racetest', pub,
                             is_private=False)
    assert status == 'ok'
    public_file = os.path.join(Config.UPLOAD_FOLDER, f'{fh}.png')
    assert os.path.exists(public_file)

    # Same bytes, different user, private. Dedup rejects it — and it must
    # not take the public file with it on the way out.
    status2, fh2 = ingest_file(_temp_upload(data), 'png', 'racetest', priv,
                               is_private=True)
    assert fh2 == fh
    assert status2 == 'duplicate'
    assert os.path.exists(public_file), \
        "the private ingest deleted the public row's file"


def test_concurrent_public_and_private_ingest_both_stay_servable(app):
    """The real interleave, driven concurrently. Whichever row wins the
    dedup, the file its row names must exist afterwards."""
    pub, priv = _user('racer5'), _user('racer6')
    data = _png_bytes()
    paths = [_temp_upload(data), _temp_upload(data)]
    results = {}
    barrier = threading.Barrier(2)

    def run(idx, uid, is_private):
        barrier.wait()
        results[idx] = ingest_file(paths[idx], 'png', 'racetest', uid,
                                   is_private=is_private)

    threads = [threading.Thread(target=run, args=(0, pub, False)),
               threading.Thread(target=run, args=(1, priv, True))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    statuses = [results[i][0] for i in (0, 1)]
    assert sorted(statuses) == ['duplicate', 'ok'], statuses

    # Exactly one row exists; the file it points at must be on disk.
    from app.media.db import get_media_by_hash
    row = get_media_by_hash(results[0][1])
    assert row is not None
    stored = os.path.join(Config.UPLOAD_FOLDER, row[1])
    assert os.path.exists(stored), f"row points at a missing file: {row[1]}"


def test_two_identical_public_ingests_are_harmless(app):
    """Same-privacy collisions were always safe — identical bytes, identical
    name — and must stay that way."""
    uid = _user('racer7')
    data = _png_bytes()
    first = ingest_file(_temp_upload(data), 'png', 'racetest', uid, False)
    second = ingest_file(_temp_upload(data), 'png', 'racetest', uid, False)
    assert first[0] == 'ok' and second[0] == 'duplicate'
    assert os.path.exists(os.path.join(Config.UPLOAD_FOLDER, f'{first[1]}.png'))
