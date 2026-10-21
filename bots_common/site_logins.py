"""Stored site logins, turned into what yt-dlp needs for one link.

The index keeps one Netscape cookies.txt per platform (app/site_logins.py,
uploaded on /admin/site-logins), and this is how both bots use them: ask
the index for the login a link needs right before resolving it, give
yt-dlp a private copy for that one run, and — once a download that used
it has worked — send back what the site rotated, so the session stays
alive between uploads.

Three details are load-bearing:

  * **A private copy per run.** yt-dlp rewrites its cookie file when it
    closes (truncate, then write), so two resolves sharing one file could
    read it empty and quietly go out logged out. mkstemp makes each copy
    0600, so another process on the box can't read the session either.
  * **Write-back only after a run that used the login and worked.** A
    failed run can end with the site having *cleared* the session, and a
    public video that downloaded without it says nothing about it. The
    index also refuses a write-back that would log the platform out, or
    that lands after an admin replaced the login (409), so a stale run can
    never undo an upload.
  * **The index's errors are the caller's.** A login lookup that fails
    raises the bot's own AuthError / IndexUnavailable, exactly like an
    identity lookup, instead of quietly resolving logged out — that would
    turn "the index was restarting" into a settled "login required" 🚫.

`index` is the bot's IndexClient: anything with `site_login(url)` and
`store_site_login(login, text)` coroutines.
"""
import contextlib
import logging
import os
import tempfile

#: Where an admin fixes a refused login — named in the failure report.
ADMIN_PAGE = '/admin/site-logins'


class StoredLogin:
    """One login as the index handed it out: its platform id and display
    name, the revision a write-back must name, and the cookies.txt text."""
    __slots__ = ('platform', 'name', 'revision', 'text')

    def __init__(self, platform, name, revision, text):
        self.platform = platform
        self.name = name
        self.revision = revision
        self.text = text

    @classmethod
    def from_json(cls, data):
        """From the index's GET /api/site-logins/cookies reply, or None
        when it is missing a field (the caller treats that as unreadable)."""
        try:
            return cls(str(data['platform']), str(data.get('name') or
                                                  data['platform']),
                       int(data['revision']), str(data['cookies']))
        except (KeyError, TypeError, ValueError):
            return None


class LoginRun:
    """The cookies one resolve runs with.

    `path` goes to resolver.resolve (None = no cookies). The caller sets
    `used` once a download that ran with them succeeded — that is what
    makes the rotated cookies worth sending back.
    """
    __slots__ = ('path', 'login', 'used')

    def __init__(self, path=None, login=None):
        self.path = path
        self.login = login
        self.used = False

    def hint(self, error):
        """What to add to a failure's report when it was a login problem,
        so the person reading it knows what an admin can do about it."""
        if getattr(error, 'kind', None) != 'auth':
            return ''
        if self.login is not None:
            return (f" (the stored {self.login.name} login was refused — an "
                    f"admin can upload a fresh one at {ADMIN_PAGE})")
        return (f" (no login is stored for that site — an admin can add one "
                f"at {ADMIN_PAGE})")


def _cookie_lines(text):
    """The cookie lines of a cookies.txt, order-free. yt-dlp rewrites the
    file with its own header comments even when nothing changed."""
    return frozenset(
        line for line in (text or '').splitlines()
        if line.strip() and (not line.startswith('#')
                             or line.startswith('#HttpOnly_')))


@contextlib.asynccontextmanager
async def cookies_for(index, url, tmpdir, fallback=None):
    """Yield the LoginRun to resolve `url` with.

    With a login stored for the link's site (or the index's "other sites"
    fallback), that is a private copy of it, deleted afterwards. Without
    one, `fallback` — the bot's mounted *_YTDLP_COOKIES file, if any —
    is passed through untouched, exactly as before logins lived in the
    index.
    """
    login = await index.site_login(url)
    if login is None:
        yield LoginRun(fallback or None)
        return
    # The resolver's own prefix, so its sweep reclaims a copy orphaned by
    # a resolve that hung past its ceiling (yt-dlp saves the jar again
    # when that thread finally closes it).
    fd, path = tempfile.mkstemp(prefix='ytdlp-cookies-', suffix='.txt',
                                dir=tmpdir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(login.text)
        run = LoginRun(path, login)
        yield run
        if run.used:
            await _write_back(index, login, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


async def _write_back(index, login, path):
    """Send the rotated cookies back. Best effort: the download already
    worked, and a lost rotation only means the next run starts from the
    slightly older copy."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except OSError:
        return
    if _cookie_lines(text) == _cookie_lines(login.text):
        return
    try:
        revision = await index.store_site_login(login, text)
    except Exception as e:
        logging.warning("site-logins: couldn't save the rotated %s login — "
                        "%s", login.platform, e)
        return
    if revision is None:
        logging.info("site-logins: kept the stored %s login — it changed "
                     "meanwhile, or the rotation would have logged it out",
                     login.platform)
    else:
        logging.info("site-logins: saved the rotated %s login (revision "
                     "%s)", login.platform, revision)

