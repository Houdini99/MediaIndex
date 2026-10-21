"""Message parsing shared by both bots: links out of message text, tag
lists out of captions, direct-media URLs, and the download progress line.

Pure stdlib so it stays unit-testable without discord.py or any Telegram
library installed. Each bot's own parsing module re-exports these next to
its platform-specific helpers.
"""
import posixpath
import re
import urllib.parse

# Links, and anything rendered as <...> — Discord mentions, channel refs and
# custom emoji — mark a message as prose, not a tag list.
_NOT_TAGS_RE = re.compile(r'https?://|<[^>]+>')

_URL_RE = re.compile(r'https?://[^\s<>]+')


def extract_urls(text):
    """http(s) URLs in a message, tolerating Discord's <no-embed> wrapping
    and trailing punctuation. Download links that only ever worked for
    the person who copied them come back as their page instead (see
    page_for_signed_link)."""
    return [page_for_signed_link(m.group(0).rstrip('>.,;:)]}'))
            for m in _URL_RE.finditer(text or '')]


# rule34video download link: /get_file/<group>/<hash>/<bucket>/<id>/<file>/
_R34_FILE_RE = re.compile(r'/get_file/\d+/[0-9a-f]+/\d+/(?P<id>\d+)/')


def page_for_signed_link(url):
    """The video page behind a per-visitor download link, else `url`.

    rule34video signs each download link (`v-acctoken`) for the address
    that loaded the page, so the link someone copies from their browser
    is a 403 from this server. The page mints a fresh one for whoever
    asks, so the bots resolve from there. Any slug reaches the page — the
    site redirects to the real one — but the link usually carries it in
    `download_filename`, which keeps logs and sources readable.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if (parts.hostname or '').lower() not in ('rule34video.com',
                                              'www.rule34video.com'):
        return url
    m = _R34_FILE_RE.match(parts.path)
    if not m:
        return url
    name = urllib.parse.parse_qs(parts.query).get('download_filename', [''])[0]
    # 'some-video-4k60fps_2160p.mp4' → 'some-video-4k60fps'
    slug = re.sub(r'[^a-z0-9-]', '',
                  re.sub(r'_\d+p?\.\w+$', '', name).lower())
    return f"https://rule34video.com/video/{m.group('id')}/{slug or 'video'}/"


def strip_urls(text):
    """Message text with every URL removed — what remains is the tag-list
    candidate ('cats,funny https://youtu.be/…' tags as 'cats,funny')."""
    return _URL_RE.sub('', text or '').replace('<>', '').strip()


def media_url_info(url, allowed_ext):
    """Filename for a URL that points directly at a media file, or None.

    The extension may sit in the path ('…/cat.gif', query strings ignored)
    or, twitter-style, in a 'format' query parameter ('…?format=jpg').
    Page URLs (Tenor pages, YouTube, articles) yield None — those are for
    embeds or the resolver, not a plain download."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https'):
        return None
    name = urllib.parse.unquote(posixpath.basename(parts.path))
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext in allowed_ext:
        return name
    fmt = urllib.parse.parse_qs(parts.query).get('format', [''])[0].lower()
    if fmt in allowed_ext:
        return f"{name or 'media'}.{fmt}"
    return None


def parse_message_tags(content, max_tags=20, max_tag_len=50):
    """Tag list from the text sent alongside media — a Discord message or a
    Telegram caption/follow-up: 'tag1,tag2,tag3' → ['tag1', 'tag2', 'tag3'].

    So that casual comments don't become tags, text only qualifies when it
    is a single line of ≤200 chars without links/mentions/emoji, and either
    contains a comma or is a single space-free token ('cats' tags, 'look at
    this lol' doesn't). Returns [] when the text does not qualify.
    """
    text = (content or '').strip()
    if not text or len(text) > 200 or '\n' in text:
        return []
    if _NOT_TAGS_RE.search(text):
        return []
    if ',' not in text and ' ' in text:
        return []
    tags = [t.strip() for t in text.split(',') if t.strip()]
    if not tags or len(tags) > max_tags or any(len(t) > max_tag_len for t in tags):
        return []
    return tags


def format_progress(done, total=None, speed=None, eta=None):
    """One log line for a running download:
    '12.3/45.6 MB (27%) · 3.2 MB/s · ETA 11s'.

    Every field except `done` may be None/unknown and is then omitted
    ('5.0 MB · 1.0 MB/s'). Bytes in, human units out.
    """
    if total:
        parts = [f"{done / 1e6:.1f}/{total / 1e6:.1f} MB ({done / total:.0%})"]
    else:
        parts = [f"{done / 1e6:.1f} MB"]
    if speed:
        parts.append(f"{speed / 1e6:.1f} MB/s")
    if eta and eta > 0:
        m, s = divmod(int(eta), 60)
        parts.append(f"ETA {f'{m}m{s:02d}s' if m else f'{s}s'}")
    return ' · '.join(parts)
