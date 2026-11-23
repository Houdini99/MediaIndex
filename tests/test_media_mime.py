"""The Content-Type media is served with, as the web image computes it.

The image is python:*-slim, which has no /etc/mime.types, so only Python's
built-in table is there. That table has no .m4v, and an .m4v answered as
application/octet-stream is not a video to the browser. A desktop's system
table maps .m4v and would hide the gap, so these tests swap in the
built-in table alone.
"""
import io
import mimetypes

import pytest

from app.db_utils import db_conn
from app.media import processing


@pytest.fixture()
def slim_mimetypes(monkeypatch):
    monkeypatch.setattr(mimetypes, '_db', mimetypes.MimeTypes())
    assert mimetypes.guess_type('x.m4v')[0] is None      # the gap is real
    processing.register_mime_types()


@pytest.fixture()
def no_thumb_worker(monkeypatch):
    monkeypatch.setattr('app.media.ingest.enqueue_video_thumb',
                        lambda *a, **kw: None)


def test_m4v_is_an_mp4_to_the_mime_table(slim_mimetypes):
    assert mimetypes.guess_type('clip.m4v')[0] == 'video/mp4'
    assert mimetypes.guess_type('clip.M4V')[0] == 'video/mp4'
    # Registering .m4v must not change what an .mp4 is called.
    assert mimetypes.guess_extension('video/mp4') == '.mp4'


@pytest.mark.parametrize('private', [False, True])
def test_an_uploaded_m4v_is_a_video_served_as_mp4(
        client, logged_in, slim_mimetypes, no_thumb_worker, private):
    payload = {'files': (io.BytesIO(b'not-really-a-video-%d' % private),
                         'clip.m4v'), 'tags_0': 'test'}
    if private:
        payload['private_0'] = '1'
    resp = client.post('/upload', data=payload,
                       content_type='multipart/form-data', follow_redirects=True)
    assert 'Successful'.encode() in resp.data

    with db_conn() as conn:
        fname, ftype = conn.execute(
            "SELECT filepath, ftype FROM media.media").fetchone()
    assert ftype == 'video'
    assert fname.endswith('.m4v.enc' if private else '.m4v')

    resp = client.get(f'/media/{fname}')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'video/mp4'
