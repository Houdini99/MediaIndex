"""What a link that names no file actually serves, and whether a link may
be fetched at all.

Plenty of media hosts serve files from URLs without an extension —
a CDN path like `https://files.example/ab/cd/<md5>` is an mp4 — so the
extension test in parsing.media_url_info misses them, and nothing else
claims them either: no yt-dlp extractor matches, and yt-dlp's metadata
probe only counts formats with a known codec and height, which a bare
file has neither of. Discord's own unfurler simply asks the
server, and so does this: one HEAD request, and the Content-Type (plus
the filename in Content-Disposition, when there is one) decides.

Both bots use it (sniff_many, at most MAX_PER_MESSAGE links each time).
That makes a bot request a URL it was handed, from inside the stack's
networks, so every hop goes through require_public first — the same bar
bots_common/download.py sets on every download.
"""
import asyncio
import contextlib
import email.message
import ipaddress
import logging
import mimetypes
import posixpath
import socket
import urllib.parse

import aiohttp

#: Redirect hops followed before giving up. Each one is re-checked, since
#: a public URL that 302s to 127.0.0.1 is the obvious way around a check
#: that only looked at what the user typed.
MAX_REDIRECTS = 5
REDIRECTS = frozenset((301, 302, 303, 307, 308))

#: A sniff is one header round-trip; anything slower is not worth waiting
#: on before the message's other paths get their turn.
SNIFF_TIMEOUT = 15

#: Links per message asked what they serve (one HEAD each, in parallel) —
#: enough for any real post, too few for a link dump to become a scan.
MAX_PER_MESSAGE = 4


class Blocked(Exception):
    """The URL does not point at the public internet. Its message is shown
    to the sender, so it says which part was refused."""


def require_public(url):
    """Refuse a URL that resolves to anything but a public address.

    Every address the hostname resolves to is checked, not just the first:
    a name with one public and one loopback record must not be a coin
    flip. `is_global` covers loopback, RFC1918, link-local (169.254.169.254
    among them), CGNAT and IPv6 unique-local in one predicate.

    Blocking (it does a DNS lookup), so callers hand it to a thread.

    This is a bar, not a wall. The address is resolved here and again by
    aiohttp when it connects, so a name that changes answers between the
    two — DNS rebinding — still gets through. Closing *that* means a
    connector that pins the checked address, which is a bigger change than
    this path is worth; what is here stops the straightforward version,
    where someone simply sends a link to an internal host.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        raise Blocked(f"I only fetch http(s) links, not {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise Blocked("that link has no host in it")
    try:
        infos = socket.getaddrinfo(host, parts.port or
                                   (443 if parts.scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise Blocked(f"I couldn't look up {host}") from e
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            logging.warning("links: refusing %s — %s resolves to %s",
                            url, host, address)
            raise Blocked(f"{host} is not a public address, so I won't "
                          f"fetch it")


def _disposition_name(value):
    """The filename a Content-Disposition header offers, or ''.

    email.message does the RFC 6266 parsing (quoting, `filename*=`). Some
    servers also percent-encode a plain `filename=`, which is unquoted
    too. Only the last path component is ever kept.
    """
    if not value:
        return ''
    msg = email.message.Message()
    msg['Content-Disposition'] = value
    name = msg.get_filename() or ''
    return posixpath.basename(urllib.parse.unquote(name).replace('\\', '/'))


def _ext(name):
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def media_name(url, headers, allowed_ext):
    """The filename to ingest a response as, or None when it isn't media
    the index takes.

    The server's own filename wins when it has an allowed extension and
    the response is media (or the octet-stream some CDNs use for
    everything). Otherwise the Content-Type names the extension and the
    URL's last path segment the stem.
    """
    ctype = (headers.get('Content-Type') or '').split(';')[0].strip().lower()
    is_media = ctype.startswith(('video/', 'image/'))
    offered = _disposition_name(headers.get('Content-Disposition'))
    if offered and _ext(offered) in allowed_ext \
            and (is_media or ctype == 'application/octet-stream'):
        return offered
    if not is_media:
        return None
    ext = next((e[1:] for e in mimetypes.guess_all_extensions(ctype)
                if e[1:] in allowed_ext), None)
    if not ext:
        return None
    stem = posixpath.basename(urllib.parse.urlsplit(url).path) or 'media'
    return f'{stem}.{ext}'


@contextlib.asynccontextmanager
async def follow_public(session, method, url, **kw):
    """Request `url`, following redirects by hand so require_public vets
    every hop before it is requested. Yields (final url, response) for the
    first answer that is not a redirect; raises Blocked for a refused
    address or more than MAX_REDIRECTS hops, and aiohttp's own errors.
    Both bots' downloads (download.fetch) and sniffs go through it."""
    send = getattr(session, method.lower())
    for _ in range(MAX_REDIRECTS + 1):
        # getaddrinfo blocks; a slow resolver must not stall the bot.
        await asyncio.to_thread(require_public, url)
        async with send(url, allow_redirects=False, **kw) as resp:
            location = resp.headers.get('Location')
            if resp.status in REDIRECTS and location:
                url = urllib.parse.urljoin(url, location)
                continue
            yield url, resp
            return
    raise Blocked(f"that link redirected more than {MAX_REDIRECTS} times")


async def sniff(session, url, allowed_ext):
    """(url, filename) for a link that serves media the index takes, else
    None. The url is the one the redirects ended at, already vetted.

    Never raises: a refused address, a timeout or an error status all mean
    "not something to download directly", and the caller moves on to its
    other paths (the Discord bot's metadata probe vets the address again
    before it reads the page).
    """
    timeout = aiohttp.ClientTimeout(total=SNIFF_TIMEOUT)
    try:
        async with follow_public(session, 'HEAD', url,
                                 timeout=timeout) as (url, resp):
            if resp.status >= 400:
                logging.info("sniff: %s answered HTTP %s", url, resp.status)
                return None
            name = media_name(url, resp.headers, allowed_ext)
            if name:
                logging.info("sniff: %s serves %s as %s", url,
                             resp.headers.get('Content-Type'), name)
                return url, name
            return None
    except Blocked as e:
        logging.info("sniff: not fetching %s — %s", url, e)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logging.info("sniff: %s failed — %s", url, type(e).__name__)
    return None


async def sniff_many(session, urls, allowed_ext):
    """{url: (fetch_url, filename)} for those of the first MAX_PER_MESSAGE
    distinct `urls` that serve media, asked in parallel."""
    urls = list(dict.fromkeys(urls))[:MAX_PER_MESSAGE]
    found = await asyncio.gather(*(sniff(session, url, allowed_ext)
                                   for url in urls))
    return {url: hit for url, hit in zip(urls, found) if hit}
