"""Site logins: the cookies the bots' yt-dlp downloads with.

YouTube, Instagram, X, Facebook and Reddit hide much of what they serve
behind a login, and a yt-dlp without cookies gets "Sign in to confirm
you're not a bot", "login required" or a bare 403 for it. An admin
uploads one Netscape cookies.txt per platform on /admin/site-logins; the
Discord and Telegram bots fetch the one a link needs right before
resolving it (GET /api/site-logins/cookies) and send back what the site
rotated during a download that worked (PUT /api/site-logins/<id>/cookies),
so a session keeps itself alive between uploads.

Three rules shape this module:

  * **Only a platform's own cookies are kept.** An upload is filtered to
    the platform's domains, so a whole-browser export filed under YouTube
    hands the bots YouTube's session and nothing else. `other` is the
    exception: it is the fallback for every site without an entry of its
    own, so it keeps everything it is given.
  * **Encrypted at rest under SECRET_KEY**, never under anything the
    database holds: a cookies.txt *is* the account, and the nightly
    pg_dump must not be a copy of it. A changed SECRET_KEY makes a stored
    login unreadable, and it is reported as such rather than guessed at.
  * **A write-back is compare-and-swap.** Every write draws a fresh
    revision from one sequence, and a bot's write-back names the revision
    it downloaded with, so it can never land on top of an upload, or bring
    back a login an admin removed, while its download was running.
"""
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import Config
from .db_utils import db_conn, db_now, ensure_pg_schema

#: Larger than any real export of one site, small enough that a pasted
#: browser profile can't be filed by accident.
MAX_BYTES = 1024 * 1024

#: The magic first line. yt-dlp is lenient about it, Python's own
#: MozillaCookieJar is not, so every stored file starts with it.
HEADER = '# Netscape HTTP Cookie File'

#: Prefix some exporters put on the domain field of an HttpOnly cookie.
#: It makes the line look like a comment, but it is a cookie.
_HTTPONLY = '#HttpOnly_'


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    domains: tuple            # host suffixes this platform's links live on
    login_cookies: tuple      # any one of these on its own domains = logged in
    hint: str = ''

    def owns(self, host):
        """True for a hostname, or a cookie domain like '.youtube.com', on
        this platform's domains — on a label boundary, so 'notyoutube.com'
        and 'x.com.evil.net' never match."""
        host = (host or '').lower().lstrip('.').rstrip('.')
        return any(host == d or host.endswith('.' + d) for d in self.domains)


PLATFORMS = (
    Platform(
        'youtube', 'YouTube',
        ('youtube.com', 'youtu.be', 'youtube-nocookie.com'),
        ('__Secure-3PSID', 'SAPISID', '__Secure-3PAPISID'),
        "Most public videos download without a login (the PO-token "
        "provider handles the bot check). Needed for age-restricted, "
        "members-only and private videos. Use a spare Google account: "
        "heavy downloading can get an account flagged."),
    Platform(
        'instagram', 'Instagram', ('instagram.com', 'instagr.am'),
        ('sessionid',),
        "Instagram wants a login for almost everything."),
    Platform(
        'twitter', 'X / Twitter', ('twitter.com', 'x.com', 't.co'),
        ('auth_token',),
        "Needed for sensitive-media posts and most videos."),
    Platform(
        'facebook', 'Facebook', ('facebook.com', 'fb.com', 'fb.watch'),
        ('c_user',),
        "Needed for non-public videos and reels."),
    Platform(
        'reddit', 'Reddit', ('reddit.com', 'redd.it', 'redditmedia.com'),
        ('reddit_session',),
        "Only needed for NSFW, quarantined and private communities."),
)

#: The fallback for every site without an entry above. It has no domains
#: of its own, so nothing is filtered out of it and it never "is logged in".
OTHER = Platform(
    'other', 'Other sites', (), (),
    "Used for any site without its own entry above — an age gate on a "
    "smaller site, say. Everything in the file is kept, so upload only the "
    "sites you mean.")

_BY_ID = {p.id: p for p in PLATFORMS + (OTHER,)}


def platform(platform_id):
    """The Platform with this id (`other` included), or None."""
    return _BY_ID.get(platform_id)


def all_platforms():
    return PLATFORMS + (OTHER,)


def platform_for_url(url):
    """The dedicated Platform a link belongs to, or None (never OTHER)."""
    try:
        host = urlsplit(url or '').hostname or ''
    except ValueError:              # malformed IPv6 literal, bad port, …
        return None
    return next((p for p in PLATFORMS if p.owns(host)), None)


# ── cookies.txt ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Cookie:
    domain: str
    name: str
    expires: int                  # unix time; 0 = a session cookie
    line: str                     # the line exactly as it is stored


def parse(text):
    """The cookies in a Netscape cookies.txt, in file order.

    Comments and blank lines are skipped; `#HttpOnly_` lines are cookies.
    A line without the format's seven tab-separated fields is skipped too,
    the same as yt-dlp does with it.
    """
    cookies = []
    for raw in (text or '').splitlines():
        line = raw.rstrip('\r')
        body = line[len(_HTTPONLY):] if line.startswith(_HTTPONLY) else line
        if not body.strip() or body.startswith('#'):
            continue
        fields = body.split('\t')
        if len(fields) != 7:
            continue
        try:
            expires = int(fields[4] or 0)
        except ValueError:
            continue
        cookies.append(Cookie(fields[0], fields[5], expires, line))
    return cookies


def clean(plat, text):
    """(stored text, kept, dropped) for a cookies.txt filed under `plat`.

    Raises ValueError, worded for the admin who uploaded it, when the file
    is too large, is not a cookies.txt at all, or holds nothing for `plat`.
    """
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError(f"That file is over {MAX_BYTES // 1024} KB — export "
                         f"only the site you mean.")
    cookies = parse(text)
    if not cookies:
        raise ValueError(
            "That doesn't look like a Netscape cookies.txt. Export one with "
            "a browser extension such as \"Get cookies.txt LOCALLY\" "
            "(Netscape format, not JSON).")
    kept = cookies if plat is OTHER else [c for c in cookies
                                          if plat.owns(c.domain)]
    if not kept:
        raise ValueError(
            f"None of those cookies belong to {plat.name} "
            f"({', '.join(plat.domains)}) — was it exported on the right "
            f"site?")
    body = '\n'.join(c.line for c in kept)
    return f"{HEADER}\n{body}\n", len(kept), len(cookies) - len(kept)


def summary(plat, text):
    """What the admin page says about a stored file: how many cookies it
    holds, whether it carries the platform's login, and when the latest
    of its login cookies runs out (None for no login / session cookies)."""
    cookies = parse(text)
    login = [c for c in cookies
             if c.name in plat.login_cookies and plat.owns(c.domain)]
    expiries = [c.expires for c in login if c.expires]
    return {
        'count': len(cookies),
        'logged_in': bool(login),
        'expires': max(expiries) if expiries else None,
        'expired': bool(expiries) and max(expiries) < time.time(),
    }


# ── Encryption at rest ─────────────────────────────────────────────────────
def _key():
    """A key of its own, derived from SECRET_KEY: rotating the secret makes
    every stored login unreadable, which is the point — nothing that only
    has the database can open one."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b'mediaindex site logins v1').derive(
                    (Config.SECRET_KEY or '').encode())


def _seal(platform_id, text):
    nonce = os.urandom(12)
    # The platform id is the associated data, so a ciphertext copied onto
    # another platform's row fails to open instead of silently serving
    # one site's session to another.
    return nonce + AESGCM(_key()).encrypt(nonce, text.encode('utf-8'),
                                          platform_id.encode())


def _unseal(platform_id, blob):
    blob = bytes(blob)
    try:
        return AESGCM(_key()).decrypt(blob[:12], blob[12:],
                                      platform_id.encode()).decode('utf-8')
    except (InvalidTag, ValueError):
        return None


# ── Storage ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Login:
    platform: Platform
    text: Optional[str]           # None: stored, but SECRET_KEY can't open it
    revision: int
    uploaded_by: Optional[str]
    uploaded_at: object
    rotated_at: object

    @property
    def readable(self):
        return self.text is not None

    @property
    def fingerprint(self):
        """Short, stable tag for a log line — never the cookies themselves."""
        return hashlib.sha256((self.text or '').encode()).hexdigest()[:8]


_SELECT = """
    SELECT s.platform, s.cookies, s.revision, u.username, s.uploaded_at,
           s.rotated_at
    FROM users.site_logins s
    LEFT JOIN users.users u ON u.id = s.uploaded_by
"""


def _row_to_login(row):
    pid, blob, revision, username, uploaded_at, rotated_at = row
    return Login(platform(pid), _unseal(pid, blob), revision, username,
                 uploaded_at, rotated_at)


def get_login(platform_id):
    ensure_pg_schema()
    with db_conn() as conn:
        row = conn.execute(_SELECT + " WHERE s.platform = %s",
                           (platform_id,)).fetchone()
    return _row_to_login(row) if row and platform(row[0]) else None


def all_logins():
    """{platform id: Login} for every stored login."""
    ensure_pg_schema()
    with db_conn() as conn:
        rows = conn.execute(_SELECT).fetchall()
    return {row[0]: _row_to_login(row) for row in rows if platform(row[0])}


def login_for_url(url):
    """The stored login to download `url` with: its platform's own, else
    the `other` fallback, else None. An unreadable login counts as none."""
    plat = platform_for_url(url)
    for pid in ((plat.id,) if plat else ()) + (OTHER.id,):
        login = get_login(pid)
        if login and login.readable:
            return login
    return None


def save_upload(platform_id, text, user_id):
    """Store an admin's upload, replacing whatever was there. Returns the
    new revision."""
    ensure_pg_schema()
    with db_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO users.site_logins
                (platform, cookies, revision, uploaded_by, uploaded_at)
            VALUES (%s, %s, nextval('users.site_logins_revision'), %s, %s)
            ON CONFLICT (platform) DO UPDATE SET
                cookies = EXCLUDED.cookies,
                revision = EXCLUDED.revision,
                uploaded_by = EXCLUDED.uploaded_by,
                uploaded_at = EXCLUDED.uploaded_at,
                rotated_at = NULL
            RETURNING revision
            """,
            (platform_id, _seal(platform_id, text), user_id, db_now()),
        ).fetchone()
    return row[0]


def save_rotation(platform_id, revision, text):
    """Replace a login with what a bot's download rotated it to — only if
    it is still exactly the revision that download started from. Returns
    the new revision, or None when the login changed or went away."""
    ensure_pg_schema()
    with db_conn() as conn:
        row = conn.execute(
            """
            UPDATE users.site_logins SET
                cookies = %s,
                revision = nextval('users.site_logins_revision'),
                rotated_at = %s
            WHERE platform = %s AND revision = %s
            RETURNING revision
            """,
            (_seal(platform_id, text), db_now(), platform_id, revision),
        ).fetchone()
    return row[0] if row else None


def delete_login(platform_id):
    ensure_pg_schema()
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM users.site_logins WHERE platform = %s",
                           (platform_id,))
        return cur.rowcount > 0
