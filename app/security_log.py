"""The security log: sign-ins, failed sign-ins, bans and account changes.

app.log is an error log that rotates away after 14 days, which is too
short to answer "was someone trying to get in, and did they?" weeks after
the fact. So these events go to their own files, one per UTC day:

    log/security/security-YYYY-MM-DD.log

Nothing rotates or deletes them; keeping or archiving old days is the
operator's call. Each line is one JSON object, so a username full of
quotes or newlines can't forge a second entry, and the files stay easy to
search with grep or jq.

Every worker appends whole lines to the day's file with one O_APPEND
write, which the kernel keeps in one piece. That needs no rotation lock
and no handler shared between processes, unlike app.log.

The attempted username is logged even when no such account exists: that
is how a guessing attack shows. It also means a password typed into the
username box ends up here, which is why the files are readable by their
owner only.
"""
import json
import logging
import os
from datetime import datetime, timezone

from flask import has_request_context, request

from .config import Config

#: Usernames and user agents are clipped to this many characters; a valid
#: username is at most 32.
_MAX_FIELD = 100


def log_dir():
    """Where the security log lives. Read at call time, so it follows
    Config.LOG_FOLDER when tests point that somewhere else."""
    return os.path.join(Config.LOG_FOLDER, 'security')


def _clip(value):
    if not isinstance(value, str):
        return value
    return value if len(value) <= _MAX_FIELD else value[:_MAX_FIELD] + '…'


def log_event(event, ip=None, ua=None, **fields):
    """Append one event to today's security log.

    `ip` and `ua` default to the current request's client address (after
    the trusted-proxy handling) and User-Agent. Fields set to None are left
    out. A write failure is reported to app.log and never breaks the
    request that caused it.
    """
    now = datetime.now(timezone.utc)
    if has_request_context():
        if ip is None:
            ip = request.remote_addr
        if ua is None:
            ua = request.headers.get('User-Agent')
    record = {'time': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'event': event,
              'ip': ip, **fields, 'ua': ua}
    record = {k: _clip(v) for k, v in record.items() if v is not None}
    line = json.dumps(record, ensure_ascii=False) + '\n'

    path = os.path.join(log_dir(), f'security-{now:%Y-%m-%d}.log')
    try:
        os.makedirs(log_dir(), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode('utf-8'))
        finally:
            os.close(fd)
    except OSError:
        logging.exception('security log: could not write %s', path)


def failed_login(user, via, known, ip=None, ua=None):
    """A sign-in that failed: count it toward the IP ban, and log it.

    `via` names the door that was tried: 'login' (the login form),
    'password-change' (a wrong current password on a password form),
    'webdav' or 'api' (a wrong app password). `known` says whether the
    username belongs to an account. When this failure is the one that
    bans the address, the ban is logged right after it.
    """
    from .auth.db import record_failed_login
    if ip is None and has_request_context():
        ip = request.remote_addr
    started_ban = record_failed_login(ip)
    log_event('login_failed', ip=ip, ua=ua, user=user, known=known, via=via)
    if started_ban:
        log_event('ip_banned', ip=ip, ua=ua, address=ip,
                  failures=Config.BAN_THRESHOLD,
                  hours=int(Config.BAN_DURATION.total_seconds() // 3600))
