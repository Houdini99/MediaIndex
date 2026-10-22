"""ffprobe / ffmpeg as bounded, killable subprocesses.

Every video this app decodes arrived from outside, and ffmpeg is not
trustworthy on media it did not produce: a malformed file can make it block
forever. All three callers run that decode on a thread other work depends
on — the upload thumbnail worker and the WebDAV staging scanner are single
shared threads, and the near-duplicate scan must stay cancellable — so every
call here has a hard deadline and dies with its process when it passes.

Calling ffmpeg directly is what makes that containment cheap: the decoder
is its own process, holding no app state, and a SIGKILL ends it. A Python
wrapper that hides ffmpeg behind a blocking read with no timeout would
leave nothing to kill short of forking the whole multi-threaded gunicorn
worker, database pool, logging locks and all.
"""
import io
import subprocess
import time

from PIL import Image

#: Executables, overridable (tests point them at stand-ins).
FFMPEG = 'ffmpeg'
FFPROBE = 'ffprobe'

#: How often a waiting call wakes to check its deadline and `keepalive`.
_POLL = 0.5


class Cancelled(Exception):
    """Raised when `keepalive` asked the call to stop."""


class Deadline:
    """One time budget shared by several calls on the same file. `left()`
    returns the seconds remaining, to pass as the next call's timeout, and
    raises TimeoutError(message) once the budget is spent."""

    def __init__(self, timeout, message):
        self._end = time.monotonic() + timeout
        self._message = message

    def left(self):
        remaining = self._end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(self._message)
        return remaining


def run(args, timeout, keepalive=None, what='ffmpeg'):
    """Run `args`, returning its stdout; kill it past `timeout` seconds.

    `keepalive`, when given, is called about twice a second while waiting;
    returning False kills the process and raises `Cancelled`.

    Raises `TimeoutError` past the deadline, `Cancelled` on cancel, and
    `RuntimeError` (carrying the tail of stderr) on a non-zero exit.
    """
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True)
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                out, err = proc.communicate(
                    timeout=min(_POLL, max(0.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f'gave up after {timeout:.0f}s — the {what} stopped '
                        'responding') from None
                if keepalive is not None and not keepalive():
                    raise Cancelled() from None
    except BaseException:
        proc.kill()
        proc.communicate()              # reap it; never leave a zombie
        raise
    if proc.returncode != 0:
        tail = err.decode('utf-8', 'replace').strip()[-300:]
        raise RuntimeError(f'{what} failed (exit {proc.returncode}): {tail}')
    return out


def duration(path, timeout, keepalive=None):
    """Container duration in seconds, or None when the file does not say."""
    out = run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', path],
              timeout, keepalive, what='video prober')
    try:
        value = float(out.decode().strip())
    except ValueError:                  # "N/A" on broken metadata
        return None
    return value if value > 0 else None


def frame_at(path, seconds, timeout, keepalive=None):
    """The frame at `seconds` into the video, as an RGB PIL image.

    ffmpeg applies the stream's rotation metadata itself, so the frame is
    upright. Raises ValueError when no frame exists there (past the end, or
    an undecodable stream).
    """
    out = run([FFMPEG, '-v', 'error', '-nostdin',
               '-ss', f'{max(0.0, seconds):.3f}', '-i', path,
               '-an', '-sn', '-frames:v', '1',
               '-f', 'image2pipe', '-c:v', 'bmp', '-'],
              timeout, keepalive, what='video decoder')
    if not out:
        raise ValueError(f'no frame at {seconds:.1f}s')
    img = Image.open(io.BytesIO(out))
    img.load()
    return img.convert('RGB')
