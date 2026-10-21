"""StreamToLogger and rotating-file handler setup.

stdout and stderr are redirected into rotating log files. That redirection
is a feedback loop waiting to happen: anything that writes a *log record*
to `sys.stderr` gets captured here, re-logged, and — if that record can
reach a stderr handler again — logged once more, forever. It bit us for
real (a corrupt video made the duplicate scanner call `logging.warning`,
which triggered an implicit `basicConfig()` whose StreamHandler pointed at
the already-redirected stderr; the recursion ended in a RecursionError
that killed the scan thread). Three defences, all required:

  1. The STDOUT/STDERR loggers do not propagate to the root logger.
  2. The root logger is given a real file handler here, so application
     `logging.*` calls have a destination and Python never falls back to
     `basicConfig()`/`lastResort`, both of which write to `sys.stderr`.
  3. StreamToLogger refuses to re-enter itself on the same thread.

Rotation is the other half of this module, and it has two traps:

  * Several `TimedRotatingFileHandler`s on one `app.log` (three loggers
    times two gunicorn workers would make six) destroy the log.
    `doRollover` does `if os.path.exists(dfn): os.remove(dfn)` before
    renaming, so at midnight the second handler deletes what the first has
    just rotated and renames the near-empty new file over it.
  * `backupCount` defaults to `0`, and the pruning branch is
    `if self.backupCount > 0`, so unless it is set, rotated files are
    never removed.

So rotation is single-writer. Within a process the three
loggers share *one* handler instance (a formatter picks the per-logger
prefix). Across processes, exactly one worker wins an `flock` and owns
rotation, while the others use `WatchedFileHandler`, which notices the file
being renamed out from under it and reopens the new one. That election is
the same pattern `app.media.staging.start_scanner()` already uses.

One more consequence of single-writer rotation: a `TimedRotatingFileHandler`
only rotates from inside `emit()`, i.e. when *its own* process logs
something. The other worker's records never trigger it, so on a quiet owner
app.log simply kept growing across days (one ran from Sep 13 to Sep 22). The
owner therefore also runs a small timer thread that rotates on the clock.

Logging to Docker's json-file driver instead would also have worked, but
`/admin/logs` reads `log/app.log*` off disk — moving the logs would take
the admin log viewer with it.
"""
import fcntl
import logging
import os
import sys
import threading
import time
from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler

#: Rotated files kept before the oldest is deleted (days, at one per day).
BACKUP_COUNT = 14

#: Seconds between the rotation owner's checks for a due rollover.
ROTATION_CHECK_INTERVAL = 60

#: Held for the process lifetime — releasing it would hand rotation to a
#: second worker while this one is still writing. Never close it.
_rotation_lock_fh = None


class StreamToLogger(object):
    """
    Fake file-like stream object that redirects writes to a logger instance.
    """
    def __init__(self, logger, stream=None, log_level=logging.INFO):
        self.logger = logger
        self.stream = stream
        self.log_level = log_level
        self.linebuf = ''
        self._local = threading.local()

    def _reentrant(self):
        """True when this write was itself produced by our own logging.

        Without this, one stray stderr handler anywhere in the process
        turns every log record into an infinite loop.
        """
        return getattr(self._local, 'busy', False)

    def write(self, buf):
        # Also write to original stream (console)
        if self.stream:
            self.stream.write(buf)

        if self._reentrant():
            return

        # Log to file
        self._local.busy = True
        try:
            temp_linebuf = self.linebuf + buf
            self.linebuf = ''
            for line in temp_linebuf.splitlines(True):
                if line[-1:] == '\n':
                    self.logger.log(self.log_level, line.rstrip())
                else:
                    self.linebuf += line
        finally:
            self._local.busy = False

    def flush(self):
        if self.linebuf != '' and not self._reentrant():
            self._local.busy = True
            try:
                self.logger.log(self.log_level, self.linebuf.rstrip())
            finally:
                self._local.busy = False
        self.linebuf = ''
        if self.stream:
            self.stream.flush()


class TaggedFormatter(logging.Formatter):
    """Per-logger record shapes from a single handler.

    One handler can hold one formatter, but the three loggers want different
    prefixes — and sharing one handler is exactly what stops the workers
    from clobbering each other's rotated files. So the formatter branches on
    the logger name instead, giving each logger its own line format.
    """

    _STREAM = logging.Formatter('%(asctime)s [%(name)s] %(message)s')
    _APP = logging.Formatter(
        '%(asctime)s [APP] %(levelname)s %(name)s: %(message)s')

    def format(self, record):
        if record.name in ('STDOUT', 'STDERR'):
            return self._STREAM.format(record)
        return self._APP.format(record)


def _claim_rotation(log_dir):
    """Become the one process allowed to rotate the shared log file.

    Returns True for the winner. Gunicorn runs two workers and both call
    configure_logging(); if both rotated, the second would delete what the
    first had just archived. The loser watches for the rename instead.

    Fails *open* (claims ownership) when the lock cannot be taken for any
    reason other than contention: a read-only or exotic filesystem must not
    leave the process with no rotation at all.
    """
    global _rotation_lock_fh
    try:
        fh = open(os.path.join(log_dir, '.logrotate.lock'), 'w')
    except OSError:
        return True
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False        # another worker owns rotation
    _rotation_lock_fh = fh  # held for the process lifetime
    return True


def build_handler(log_path):
    """The single file handler this process writes every record through.

    The rotation owner gets a real `TimedRotatingFileHandler`; every other
    worker gets a `WatchedFileHandler`, which re-opens the file when it sees
    the inode change — i.e. when the owner rotates it away.
    """
    if _claim_rotation(os.path.dirname(log_path)):
        handler = TimedRotatingFileHandler(
            log_path, when='midnight', interval=1, backupCount=BACKUP_COUNT)
        # Matches the default suffix for when='midnight', so the handler's
        # own extMatch still recognises rotated files and prunes them.
        handler.suffix = '%Y-%m-%d'
    else:
        handler = WatchedFileHandler(log_path)
    handler.setFormatter(TaggedFormatter())
    return handler


def rotate_if_due(handler):
    """Roll `handler`'s file over if its rotation time has passed, without
    waiting for this process to log something. Returns True if it rotated.

    Runs under the handler's own lock, the same one `emit()` holds while it
    makes the identical check, so the two can never rotate twice.
    """
    handler.acquire()
    try:
        # `record` is unused by TimedRotatingFileHandler.shouldRollover.
        if handler.shouldRollover(None):
            handler.doRollover()
            return True
        return False
    finally:
        handler.release()


def _start_rotation_timer(handler):
    def loop():
        while True:
            time.sleep(ROTATION_CHECK_INTERVAL)
            try:
                rotate_if_due(handler)
            except Exception:
                # Nowhere safe to log this — it is the log file that failed.
                pass
    threading.Thread(target=loop, name='log-rotation', daemon=True).start()


def configure_logging(base_dir):
    """Set up rotating file logging and redirect stdout/stderr through it."""
    log_dir = os.path.join(base_dir, 'log')
    os.makedirs(log_dir, exist_ok=True)

    # Set up logger for stdout
    stdout_logger = logging.getLogger('STDOUT')
    stdout_logger.setLevel(logging.INFO)

    # Set up logger for stderr
    stderr_logger = logging.getLogger('STDERR')
    stderr_logger.setLevel(logging.ERROR)

    # ONE handler, shared by all three loggers. Two handlers on the same
    # file means two rotations of the same file, and the second destroys the
    # first's output — that is the whole bug this replaces.
    log_path = os.path.join(log_dir, "app.log")
    handler = build_handler(log_path)
    if isinstance(handler, TimedRotatingFileHandler):
        _start_rotation_timer(handler)

    # Add handlers to loggers. propagate=False is load-bearing: these two
    # loggers are fed *from* sys.stdout/sys.stderr, so letting their records
    # bubble up to a root logger that writes to stderr closes the loop.
    # It also keeps the shared handler from emitting each record twice.
    stdout_logger.addHandler(handler)
    stdout_logger.propagate = False
    stderr_logger.addHandler(handler)
    stderr_logger.propagate = False

    # The root logger gets the same file handler so application `logging.*`
    # calls land in app.log directly. Without a root handler Python falls
    # back to basicConfig()/lastResort, which write to sys.stderr — i.e.
    # straight back into StreamToLogger.
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    # Any stderr-bound handler another library adds to root later is
    # harmless: defence 1 stops the record coming back, and defence 3 stops
    # the stream re-entering itself. So we leave other handlers alone.

    # Redirect stdout and stderr to loggers while keeping console output
    sys.stdout = StreamToLogger(stdout_logger, sys.stdout, logging.INFO)
    sys.stderr = StreamToLogger(stderr_logger, sys.stderr, logging.ERROR)
