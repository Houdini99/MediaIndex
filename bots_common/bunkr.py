"""Bunkr links, turned into the file they show.

A Bunkr link is a page, never the file — even `https://bunkr.cr/f/<name>.mp4`,
whose extension makes it look like one. Fetched as it stands, it is 40 KB
of HTML that the index would store as a broken video. The file itself sits
on Bunkr's CDN behind a token that expires after a couple of hours, so no
link to it can be kept: it has to be asked for at download time, the way
the page's own Download button does it —

  1. the page names the file's id (its `https://dl.<host>/file/<id>` link)
     and the signing service (`signUrl`);
  2. `POST https://dl.<host>/api/_001_v2 {"id": "<id>"}` answers with the
     CDN host, the file's path there and its original name;
  3. `GET <signUrl>?path=<path>` answers with a token and its expiry,
     which go on the CDN URL as `token` and `ex`.

The `dl.<host>/file/<id>` page (the Download button's target, which people
copy too) carries the id in its URL and the signing service in its
script, so it takes the same three steps.

download.fetch() runs this for every Bunkr link it is handed, so the token
is always minted right before the transfer: a Telegram link parked while
its sender picks tags, or a Discord retry hours later, still works. The
link without an extension (`/f/Xy7AbCdEfGh2K`) is named by `sniff`, which
asks the API without signing anything.

An album link (`/a/<id>`) is read once, in the page's "advanced" view,
which carries the album's whole file list as data rather than only what
fits on a page. Each file in it becomes an ordinary file link
(`/f/<slug>`) and takes the walk above when it is downloaded. read_album
keeps the files the index takes, in album order and up to the caller's
limit, and says what it left out.

Every request goes through sniff.follow_public, and so does the download
itself, so nothing on a page can point the bot at the stack's own
services. When Bunkr changes its pages (it does, every few months), the
step that broke says so in the log and the sender is told the link could
not be read, rather than handed a web page as a video.
"""
import asyncio
import html as htmllib
import json
import logging
import re
import urllib.parse

import aiohttp

from . import sniff
from .parsing import file_ext

#: Bunkr changes domain often (bunkr.cr, .si, .la, .ru, bunkrr.su, …);
#: the name stays. Subdomains cover dl.<host> and get.bunkrr.su.
_HOST_RE = re.compile(r'^(?:[a-z0-9-]+\.)?bunkrr?\.[a-z]{2,}$')

#: A file page (/f/; the older /v/ and /i/ redirect to it) or the Download
#: button's page (/file/<id>).
_PAGE_RE = re.compile(r'^/[fvi]/[^/]+/?$')
_DL_PAGE_RE = re.compile(r'^/file/(?P<id>\d+)/?$')
#: An album: many files (read_album).
_ALBUM_RE = re.compile(r'^/a/[^/]+/?$')

#: The page's signing service: `var signUrl = "…"` on a file page,
#: `const SIGN_SERVICE_URL = '…'` on the Download button's page.
_SIGN_RE = re.compile(
    r'''\b(?:signUrl|SIGN_SERVICE_URL)\s*=\s*["'](?P<url>[^"']+)["']''')
#: The Download button on a file page.
_DL_LINK_RE = re.compile(
    r'''href=["'](?P<origin>https://[^"'/]+)/file/(?P<id>\d+)/?["']''')

#: Seconds for each of the three small requests. The download itself runs
#: on the bot session's own timeouts.
LOOKUP_TIMEOUT = 30

#: The most read from a page or an API answer. A file page is ~40 KB; the
#: host check lets through any bunkr.<tld>, which anyone can register.
MAX_PAGE_BYTES = 2 * 1024 * 1024
#: The most read from an album's advanced view, which lists every file at
#: ~550 bytes each: room for some 15 000.
MAX_ALBUM_BYTES = 8 * 1024 * 1024

#: Where the advanced view's file list starts, and one entry of it — a JS
#: object literal, `{ id: 3004946, original: "IMG_1.jpg", slug: "…", … }`.
#: The entry pattern skips over quoted strings, so a brace or a quote
#: inside a filename can't end it early.
_ALBUM_FILES_RE = re.compile(r'\bwindow\.albumFiles\s*=\s*\[')
_ALBUM_ENTRY_RE = re.compile(
    r'\s*\{(?P<body>(?:[^{}"]|"(?:[^"\\]|\\.)*")*)\}\s*,?')
_ENTRY_FIELD_RE = re.compile(
    r'(?P<key>\w+)\s*:\s*(?P<value>"(?:[^"\\]|\\.)*"|[^,\s}]+)')
_TITLE_RE = re.compile(r'<title>(?P<title>.*?)</title>', re.S | re.I)


class Unreadable(Exception):
    """Bunkr answered, but not with what the Download button expects — the
    site changed, or the link is not a file. The message is shown to the
    sender."""


def _bunkr_path(url):
    """The path of an http(s) link on a Bunkr host, else None."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') \
            or not _HOST_RE.match((parts.hostname or '').lower()):
        return None
    return parts.path


def is_file_link(url):
    """True for a link to one Bunkr file: its page or its Download page."""
    path = _bunkr_path(url)
    return bool(path and (_PAGE_RE.match(path) or _DL_PAGE_RE.match(path)))


def is_album_link(url):
    """True for a link to a Bunkr album."""
    path = _bunkr_path(url)
    return bool(path and _ALBUM_RE.match(path))


def page_refs(html, page_url):
    """(api_url, file_id, sign_url) from a file page or a Download page.

    `page_url` is where the page was finally fetched from: a Download page
    names its file in its own URL and serves the API itself, while a file
    page links to the Download page, which says both.
    """
    sign = _SIGN_RE.search(html or '')
    parts = urllib.parse.urlsplit(page_url)
    own = _DL_PAGE_RE.match(parts.path)
    if own:
        origin, file_id = f'{parts.scheme}://{parts.netloc}', own.group('id')
    else:
        link = _DL_LINK_RE.search(html or '')
        if not link:
            raise Unreadable("there is no file on that Bunkr page (no "
                             "Download link)")
        origin, file_id = link.group('origin'), link.group('id')
    if not sign:
        raise Unreadable("that Bunkr page has changed — I can't find how it "
                         "signs its downloads")
    sign_url = sign.group('url').replace('\\/', '/')
    if not sign_url.startswith('https://'):
        raise Unreadable("that Bunkr page has changed — its signing service "
                         "is not an https link")
    return f'{origin}/api/_001_v2', file_id, sign_url


def api_file(payload):
    """(unsigned CDN URL, original filename) from the API's answer."""
    if not isinstance(payload, dict):
        payload = {}
    host, path = payload.get('mediafiles'), payload.get('path')
    if not (isinstance(host, str) and host.startswith('https://')
            and isinstance(path, str) and path.startswith('/')):
        raise Unreadable("Bunkr's download API answered something I don't "
                         "understand — the site may have changed")
    url = host.rstrip('/') + urllib.parse.quote(path, safe='/%')
    name = payload.get('original')
    if not isinstance(name, str) or not name.strip():
        name = urllib.parse.unquote(path.rsplit('/', 1)[-1])
    return url, name.strip()


def signed(url, answer):
    """The CDN URL with the signing service's token on it."""
    if not isinstance(answer, dict) or not answer.get('token') \
            or not answer.get('ex'):
        raise Unreadable("Bunkr's signing service answered something I don't "
                         "understand — the site may have changed")
    query = urllib.parse.urlencode({'token': answer['token'],
                                    'ex': answer['ex']})
    return f'{url}?{query}'


async def _read(resp, limit=None):
    """The body of a successful response, refusing anything implausibly
    big for a page or a JSON answer."""
    resp.raise_for_status()
    limit = limit or MAX_PAGE_BYTES
    body = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > limit:
            raise Unreadable("that Bunkr page is far too big to be one")
    return bytes(body)


def _json(body, what):
    try:
        return json.loads(body)
    except ValueError:
        raise Unreadable(f"Bunkr's {what} did not answer with JSON — the "
                         f"site may have changed") from None


async def lookup(session, url):
    """(unsigned CDN URL, original filename, sign_url) for a Bunkr link —
    the page and the API, not the signing. Raises Unreadable, sniff.Blocked
    and aiohttp's own errors."""
    timeout = aiohttp.ClientTimeout(total=LOOKUP_TIMEOUT)
    async with sniff.follow_public(session, 'GET', url,
                                   timeout=timeout) as (page_url, resp):
        html = (await _read(resp)).decode('utf-8', 'replace')
    api_url, file_id, sign_url = page_refs(html, page_url)
    async with sniff.follow_public(session, 'POST', api_url,
                                   json={'id': file_id},
                                   timeout=timeout) as (_, resp):
        cdn_url, name = api_file(_json(await _read(resp), 'download API'))
    return cdn_url, name, sign_url


async def file_url(session, url):
    """A freshly signed URL for the file behind a Bunkr link, ready for
    download.fetch to stream. Raises Unreadable, sniff.Blocked and
    aiohttp's own errors."""
    cdn_url, name, sign_url = await lookup(session, url)
    # As the page does it: the decoded path, encodeURIComponent'd.
    path = urllib.parse.unquote(urllib.parse.urlsplit(cdn_url).path)
    query = 'path=' + urllib.parse.quote(path, safe='')
    async with sniff.follow_public(
            session, 'GET', f'{sign_url}?{query}',
            timeout=aiohttp.ClientTimeout(total=LOOKUP_TIMEOUT)) as (_, resp):
        answer = _json(await _read(resp), 'signing service')
    logging.info("bunkr: %s is %s on %s", url, name,
                 urllib.parse.urlsplit(cdn_url).hostname)
    return signed(cdn_url, answer)


async def sniff_link(session, url, allowed_ext):
    """(url, filename) when a Bunkr link holds media the index takes, else
    None — sniff.sniff's contract, for the link that names no file. The
    URL handed back is the link itself: the file is only ever fetched with
    a token minted at download time.

    Never raises: like a sniff, a failure here means "not something to
    download directly", and the bot's other paths get their turn.
    """
    try:
        _, name, _ = await lookup(session, url)
    except Unreadable as e:
        logging.info("bunkr: %s — %s", url, e)
        return None
    except sniff.Blocked as e:
        logging.info("bunkr: not fetching %s — %s", url, e)
        return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logging.info("bunkr: %s failed — %s", url, type(e).__name__)
        return None
    if file_ext(name) not in allowed_ext:
        logging.info("bunkr: %s is %s, which the index does not take",
                     url, name)
        return None
    return url, name


# ── Albums ────────────────────────────────────────────────────────────────
def _field(raw):
    """One value of an album entry: a JS string (JSON's escapes are the
    ones Bunkr uses) or a bare number."""
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    return raw.strip()


def album_entries(html):
    """Every file an album's advanced view lists, as dicts of its fields
    (`slug`, `original`, `size`, …), in album order."""
    start = _ALBUM_FILES_RE.search(html or '')
    if not start:
        raise Unreadable("that Bunkr album has changed — I can't find its "
                         "list of files")
    entries, pos = [], start.end()
    while (m := _ALBUM_ENTRY_RE.match(html, pos)):
        entries.append({f.group('key'): _field(f.group('value'))
                        for f in _ENTRY_FIELD_RE.finditer(m.group('body'))})
        pos = m.end()
    return entries


def album_title(html):
    """The album's name, without the site's ' | Bunkr' suffix."""
    m = _TITLE_RE.search(html or '')
    if not m:
        return ''
    title = ' '.join(htmllib.unescape(m.group('title')).split())
    return re.sub(r'\s*\|\s*Bunkr\s*$', '', title, flags=re.I)


class Album:
    """What one album link holds for the index.

    `files` is (file link, filename) for the images and videos the index
    takes, in album order and at most the caller's limit; `over_limit` is
    how many more there were, and `unsupported` how many files were of a
    type the index doesn't take. When the album couldn't be read, or holds
    nothing the index takes, `files` is empty and `reason` says why.
    """
    __slots__ = ('url', 'title', 'files', 'total', 'unsupported',
                 'over_limit', 'reason')

    def __init__(self, url, title='', files=(), total=0, unsupported=0,
                 over_limit=0, reason=None):
        self.url = url
        self.title = title
        self.files = list(files)
        self.total = total
        self.unsupported = unsupported
        self.over_limit = over_limit
        self.reason = reason

    def note(self):
        """One line for the sender: why nothing comes of the album, or
        what was left out of it. None when every file is taken."""
        if self.reason:
            return self.reason
        name = f'“{self.title}”' if self.title else 'the album'
        left = []
        if self.over_limit:
            media = len(self.files) + self.over_limit
            left.append(f"taking the first {len(self.files)} of the {media} "
                        f"images and videos in {name} (the album limit)")
        if self.unsupported:
            what = 'file' if self.unsupported == 1 else 'files'
            left.append(f"skipping {self.unsupported} {what} in {name} that "
                        f"the index doesn't take")
        return '; '.join(left) or None


async def read_album(session, url, allowed_ext, limit):
    """The Album behind a Bunkr album link: its files the index takes
    (`allowed_ext`), at most `limit` of them. One request — the advanced
    view lists every file — and the files' own downloads happen later,
    one file link at a time.

    Never raises: a failure comes back as an Album with no files and its
    `reason`, logged here, so the bot only has to word it.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        advanced = urllib.parse.urlunsplit(parts._replace(query='advanced=1',
                                                          fragment=''))
        async with sniff.follow_public(
                session, 'GET', advanced,
                timeout=aiohttp.ClientTimeout(total=LOOKUP_TIMEOUT)) \
                as (page_url, resp):
            html = (await _read(resp, MAX_ALBUM_BYTES)).decode('utf-8',
                                                               'replace')
        entries = album_entries(html)
    except (Unreadable, sniff.Blocked) as e:
        reason = str(e)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        status = getattr(e, 'status', None)
        reason = (f"Bunkr answered HTTP {status} for that album" if status
                  else f"couldn't read that Bunkr album ({type(e).__name__})")
    else:
        return _album(url, page_url, html, entries, allowed_ext, limit)
    logging.info("bunkr: album %s — %s", url, reason)
    return Album(url, reason=reason)


def _album(url, page_url, html, entries, allowed_ext, limit):
    """The Album for a parsed advanced view."""
    origin = '{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(page_url))
    media = []
    for entry in entries:
        slug = entry.get('slug') or entry.get('name')
        if not isinstance(slug, str) or not slug.strip():
            continue
        name = entry.get('original')
        if not isinstance(name, str) or not name.strip():
            name = slug
        name = name.replace('/', '_').replace('\\', '_').strip()
        if file_ext(name) in allowed_ext:
            media.append((f"{origin}/f/{urllib.parse.quote(slug, safe='')}",
                          name))
    title = album_title(html)
    album = Album(url, title, media[:limit], len(entries),
                  unsupported=len(entries) - len(media),
                  over_limit=max(0, len(media) - limit))
    if not media:
        name = f'“{title}”' if title else 'that album'
        album.reason = (f"{name} has no images or videos the index takes"
                        if entries else f"{name} is empty")
    logging.info("bunkr: album %s (%s) — %d files, %d the index takes, "
                 "taking %d", url, title or '?', len(entries), len(media),
                 len(album.files))
    return album

