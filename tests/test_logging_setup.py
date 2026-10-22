"""Regression guard for the stdout/stderr redirection, and for rotation.

configure_logging() replaces sys.stdout/sys.stderr with objects that turn
writes into log records. If a log record can reach sys.stderr again, that
is an infinite loop — and it happened in production: one logging.warning()
from a background thread recursed until RecursionError and killed the
thread mid-job. The first group of tests pins those three defences.

The second group pins rotation, which was destroying the very logs it
archived: three handlers per process (times two gunicorn workers) all
rotating one file, where `doRollover` deletes an existing destination
before renaming — so each rollover threw away what the previous handler had
just written. `backupCount` was also unset, so nothing was ever pruned.
"""
import fcntl
import importlib
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler

import pytest


@pytest.fixture()
def logfile(tmp_path):
    """Run configure_logging against a temp dir and restore global state."""
    from app.logging_setup import configure_logging

    saved_out, saved_err = sys.stdout, sys.stderr
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_named = {
        name: (list(logging.getLogger(name).handlers),
               logging.getLogger(name).propagate)
        for name in ('STDOUT', 'STDERR')
    }

    configure_logging(str(tmp_path / 'log'))
    # pytest reinstalls its own capture objects on sys.stdout/sys.stderr
    # between fixture setup and the test body, so hand the wrappers that
    # configure_logging built to the test directly.
    yield tmp_path / 'log' / 'app.log', sys.stdout, sys.stderr

    logging.shutdown()
    sys.stdout, sys.stderr = saved_out, saved_err
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, (handlers, propagate) in saved_named.items():
        named = logging.getLogger(name)
        named.handlers[:] = handlers
        named.propagate = propagate
    importlib.invalidate_caches()


def test_root_logging_does_not_recurse_into_the_redirected_stderr(logfile):
    path, _out, _err = logfile
    # Before the fix this raised RecursionError: logging.warning() called
    # basicConfig(), whose StreamHandler pointed at the redirected stderr.
    logging.warning('dupes: fingerprint failed for media %s: %s', 1709, 'boom')
    for handler in logging.getLogger().handlers:
        handler.flush()

    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert 'fingerprint failed for media 1709' in lines[0]
    assert 'ERROR:STDERR:' not in lines[0]


def test_exception_tracebacks_survive_intact(logfile):
    path, _out, _err = logfile
    try:
        raise ValueError('boom')
    except ValueError:
        logging.exception('scan job failed')
    for handler in logging.getLogger().handlers:
        handler.flush()

    body = path.read_text()
    assert 'scan job failed' in body
    assert 'ValueError: boom' in body
    assert body.count('scan job failed') == 1


def test_stdout_and_stderr_are_still_captured(logfile):
    path, out, err = logfile
    out.write('hello from stdout\n')
    err.write('hello from stderr\n')
    for name in ('STDOUT', 'STDERR'):
        for handler in logging.getLogger(name).handlers:
            handler.flush()

    body = path.read_text()
    assert '[STDOUT] hello from stdout' in body
    assert '[STDERR] hello from stderr' in body


def test_redirect_loggers_do_not_propagate(logfile):
    assert logging.getLogger('STDOUT').propagate is False
    assert logging.getLogger('STDERR').propagate is False


def test_root_gets_a_file_handler(logfile):
    """Without one, logging falls back to basicConfig()/lastResort — both of
    which write to the redirected sys.stderr."""
    assert any(isinstance(h, logging.FileHandler)
               for h in logging.getLogger().handlers)


def test_the_redirected_stream_refuses_to_re_enter_itself(logfile):
    """Defence 3: even a stderr-bound handler on root cannot start a loop."""
    path, _out, err = logfile
    root = logging.getLogger()
    stray = logging.StreamHandler(err)
    root.addHandler(stray)
    try:
        logging.warning('one message only')
        for handler in root.handlers:
            handler.flush()
    finally:
        root.removeHandler(stray)

    assert path.read_text().count('one message only') <= 2


# ── Rotation ───────────────────────────────────────────────────────────────
@pytest.fixture()
def fresh_lock(monkeypatch):
    """Clear the process-wide rotation-lock handle between tests."""
    import app.logging_setup as ls
    monkeypatch.setattr(ls, '_rotation_lock_fh', None)
    return ls


def test_the_three_loggers_share_one_handler(logfile):
    """The fix, stated directly: two handlers on one file means two
    rotations of that file, and the second destroys the first's output.

    Asserts the handler is the *same object* on all three loggers rather
    than counting file handlers on root — the test runner attaches its own,
    and a global count would be measuring pytest, not this module.
    """
    def file_handlers(name):
        return [h for h in logging.getLogger(name).handlers
                if isinstance(h, logging.FileHandler)]

    out, err = file_handlers('STDOUT'), file_handlers('STDERR')
    assert len(out) == 1 and len(err) == 1
    assert out[0] is err[0], "STDOUT and STDERR rotate the file independently"
    assert any(h is out[0] for h in file_handlers(None)), \
        "root rotates the file independently of the stream loggers"


def test_rotation_prunes_old_files(fresh_lock, tmp_path):
    """backupCount was unset (defaulting to 0), and the pruning branch is
    `if self.backupCount > 0` — so 68 files had piled up since May."""
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    handler = fresh_lock.build_handler(str(log_dir / 'app.log'))
    assert handler.backupCount == fresh_lock.BACKUP_COUNT > 0

    # Stand-ins for an accumulated backlog, named the way the handler does.
    for day in range(1, 26):
        (log_dir / f'app.log.2026-05-{day:02d}').write_text('old\n')
    handler.emit(logging.LogRecord(
        'x', logging.INFO, __file__, 1, 'current', None, None))
    handler.doRollover()
    handler.close()

    rotated = sorted(p.name for p in log_dir.glob('app.log.*'))
    assert len(rotated) <= fresh_lock.BACKUP_COUNT + 1  # +1 for today's
    assert 'app.log.2026-05-01' not in rotated, "oldest should be pruned first"


def test_rollover_preserves_the_archived_content(fresh_lock, tmp_path):
    """The core regression: what gets archived must survive being archived.

    With two handlers on one file, the second doRollover deleted the file
    the first had just created and renamed the near-empty replacement over
    it — losing most of the day.
    """
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    handler = fresh_lock.build_handler(str(log_dir / 'app.log'))
    handler.emit(logging.LogRecord(
        'x', logging.INFO, __file__, 1, 'must survive rotation', None, None))
    handler.doRollover()
    handler.close()

    archived = [p for p in log_dir.glob('app.log.*') if p.suffix != '.lock']
    assert len(archived) == 1
    assert 'must survive rotation' in archived[0].read_text()


def test_owner_rotates_on_the_clock_without_logging(fresh_lock, tmp_path):
    """The rotating handler only checked the time inside emit(), so when the
    other worker did all the logging the file never rotated — app.log ran
    from Sep 13 to Sep 22. rotate_if_due() is the timer thread's check."""
    import time
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    handler = fresh_lock.build_handler(str(log_dir / 'app.log'))
    handler.emit(logging.LogRecord(
        'x', logging.INFO, __file__, 1, 'yesterday', None, None))

    assert fresh_lock.rotate_if_due(handler) is False    # not due yet
    handler.rolloverAt = int(time.time()) - 1            # midnight has passed
    assert fresh_lock.rotate_if_due(handler) is True
    assert fresh_lock.rotate_if_due(handler) is False    # rescheduled
    handler.close()

    archived = [p for p in log_dir.glob('app.log.*') if p.suffix != '.lock']
    assert len(archived) == 1 and 'yesterday' in archived[0].read_text()
    assert (log_dir / 'app.log').read_text() == ''


def test_rotation_owner_gets_the_rotating_handler(fresh_lock, tmp_path):
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    handler = fresh_lock.build_handler(str(log_dir / 'app.log'))
    assert isinstance(handler, TimedRotatingFileHandler)
    handler.close()


def test_second_worker_watches_instead_of_rotating(fresh_lock, tmp_path):
    """Gunicorn runs two workers. The one that loses the lock must not
    rotate — it reopens the file when the owner renames it away."""
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    # Stand in for the worker that already owns rotation.
    holder = open(log_dir / '.logrotate.lock', 'w')
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        handler = fresh_lock.build_handler(str(log_dir / 'app.log'))
        assert isinstance(handler, WatchedFileHandler)
        assert not isinstance(handler, TimedRotatingFileHandler)
        handler.close()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_watcher_reopens_after_the_owner_rotates(fresh_lock, tmp_path):
    """The property that makes the loser safe: once the owner renames
    app.log away, the watcher must write to the *new* file, not the
    archived inode."""
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    log_path = log_dir / 'app.log'

    holder = open(log_dir / '.logrotate.lock', 'w')
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        watcher = fresh_lock.build_handler(str(log_path))
        watcher.emit(logging.LogRecord(
            'x', logging.INFO, __file__, 1, 'before', None, None))
        watcher.flush()
        # The owner rotates the file out from under the watcher.
        os.rename(log_path, log_dir / 'app.log.2026-07-26')
        watcher.emit(logging.LogRecord(
            'x', logging.INFO, __file__, 1, 'after', None, None))
        watcher.close()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert 'after' in log_path.read_text(), "watcher kept the stale inode"
    assert 'before' in (log_dir / 'app.log.2026-07-26').read_text()


def test_stream_and_app_records_keep_their_prefixes(fresh_lock, tmp_path):
    """One handler now serves three loggers, so the formatter carries the
    distinction the three separate formatters used to."""
    log_dir = tmp_path / 'log'
    log_dir.mkdir()
    handler = fresh_lock.build_handler(str(log_dir / 'app.log'))
    for name, message in (('STDOUT', 'out line'), ('STDERR', 'err line'),
                          ('app.media', 'app line')):
        handler.emit(logging.LogRecord(
            name, logging.WARNING, __file__, 1, message, None, None))
    handler.close()

    body = (log_dir / 'app.log').read_text()
    assert '[STDOUT] out line' in body
    assert '[STDERR] err line' in body
    assert '[APP] WARNING app.media: app line' in body
