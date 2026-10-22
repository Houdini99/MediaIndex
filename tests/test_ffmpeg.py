"""app/media/ffmpeg.py and the video thumbnailer built on it.

The property that matters operationally: a decoder that never returns must
not be able to wedge its caller. The upload thumbnail worker and the WebDAV
staging scanner are *single* shared threads, so an unbounded decode there
stops video thumbnails, or the whole staging queue, for every user until
the container restarts. The hang cases use a stand-in ffmpeg that sleeps;
the rest decode a real clip made with the system ffmpeg.
"""
import os
import shutil
import subprocess
import time

import pytest
from PIL import Image

from app.media import ffmpeg, processing
from app.media.processing import gen_video_thumb, is_placeholder_thumb


# ── run() ──────────────────────────────────────────────────────────────────
def test_run_returns_stdout():
    assert ffmpeg.run(['printf', 'hello'], timeout=10) == b'hello'


def test_run_kills_a_hung_process_at_the_deadline():
    started = time.time()
    with pytest.raises(TimeoutError, match='stopped responding'):
        ffmpeg.run(['sleep', '300'], timeout=1, what='video decoder')
    assert time.time() - started < 10


def test_keepalive_can_cancel():
    with pytest.raises(ffmpeg.Cancelled):
        ffmpeg.run(['sleep', '300'], timeout=60, keepalive=lambda: False)


def test_keepalive_returning_true_does_not_cancel():
    assert ffmpeg.run(['printf', 'ok'], timeout=10,
                      keepalive=lambda: True) == b'ok'


def test_failure_carries_the_stderr_tail():
    with pytest.raises(RuntimeError, match='exit 3.*boom'):
        ffmpeg.run(['sh', '-c', 'echo boom >&2; exit 3'], timeout=10)


def test_a_killed_process_is_reaped(monkeypatch):
    started = []
    real_popen = subprocess.Popen

    def recording_popen(*a, **kw):
        started.append(real_popen(*a, **kw))
        return started[-1]

    monkeypatch.setattr(ffmpeg.subprocess, 'Popen', recording_popen)
    with pytest.raises(TimeoutError):
        ffmpeg.run(['sleep', '300'], timeout=1)
    assert started[0].returncode is not None, "killed child left as a zombie"


# ── Real decoding ──────────────────────────────────────────────────────────
def test_duration_of_a_real_clip(sample_video):
    assert ffmpeg.duration(sample_video, timeout=30) == pytest.approx(4, abs=0.1)


def test_frame_at_returns_an_rgb_frame(sample_video):
    img = ffmpeg.frame_at(sample_video, 1.0, timeout=30)
    assert img.mode == 'RGB' and img.size == (320, 240)


def test_frame_past_the_end_is_a_value_error(sample_video):
    with pytest.raises(ValueError):
        ffmpeg.frame_at(sample_video, 60.0, timeout=30)


def test_unreadable_file_is_a_runtime_error(tmp_path):
    bogus = tmp_path / 'bogus.mp4'
    bogus.write_bytes(b'not a video')
    if not shutil.which('ffprobe'):
        pytest.skip('ffprobe not installed')
    with pytest.raises(RuntimeError):
        ffmpeg.duration(str(bogus), timeout=30)


# ── gen_video_thumb ────────────────────────────────────────────────────────
def test_thumbnail_of_a_real_clip(app, sample_video, tmp_path):
    dst = tmp_path / 'thumb.png'
    assert gen_video_thumb(sample_video, str(dst)) == str(dst)
    with Image.open(dst) as img:
        assert img.size == (150, 113)
    assert not is_placeholder_thumb(str(dst))


def test_thumb_falls_back_to_placeholder_on_a_hang(app, hanging_ffmpeg, tmp_path):
    """A wedged ffmpeg must still leave a thumbnail behind: both callers
    have already written a DB row pointing at this filename."""
    dst = tmp_path / 'thumb.png'
    started = time.time()
    gen_video_thumb('x.mp4', str(dst), timeout=1)
    assert time.time() - started < 10, "the caller was held past the deadline"
    assert is_placeholder_thumb(str(dst))


def test_thumb_falls_back_to_placeholder_on_garbage(app, tmp_path):
    src = tmp_path / 'garbage.mp4'
    src.write_bytes(os.urandom(4096))
    dst = tmp_path / 'thumb.png'
    gen_video_thumb(str(src), str(dst), timeout=30)
    assert is_placeholder_thumb(str(dst))


def test_no_moviepy_anywhere():
    """Video decoding never goes through moviepy, which hides ffmpeg behind
    a read with no timeout. Nothing may quietly import it."""
    import sys
    import app.admin.dupes  # noqa: F401 — every video caller is loaded
    import app.media.staging  # noqa: F401
    import app.media.tasks  # noqa: F401
    assert 'moviepy' not in sys.modules
    assert not hasattr(processing, 'gen_video_thumb_isolated')


def test_a_deadline_counts_down_and_then_raises_its_message():
    deadline = ffmpeg.Deadline(30, 'out of time')
    assert 29 < deadline.left() <= 30
    spent = ffmpeg.Deadline(0, 'no frame decoded within 0s')
    with pytest.raises(TimeoutError, match='no frame decoded within 0s'):
        spent.left()
