"""Stopgaps for yt-dlp extractors that a site redesign broke, until
upstream ships a fix.

Each one wraps the built-in extractor rather than replacing it, and only
steps in when the built-in comes back with no formats — so the day
yt-dlp fixes the site itself, the stopgap goes quiet instead of second-
guessing it. Its log line ("ytdlp_fixes: … stepped in") is how to tell:
once that stops appearing after a yt-dlp update, delete the stopgap.

The parsing is plain stdlib, so it is testable without yt-dlp installed;
apply() is the only part that touches yt-dlp, and it is a no-op when the
extractor it patches isn't there.
"""
import html
import logging
import re

# ── rule34video ───────────────────────────────────────────────────────────
# yt-dlp/yt-dlp#17792. The site's 2026-10 redesign put an icon before each
# download link's label and a size after it:
#
#   <a … href="…/1234567_2160p.mp4/?v-acctoken=…&download=true&…" …>
#     <svg …>…</svg>MP4 2160p<span class="vp-sheet__meta">125 MB</span></a>
#
# yt-dlp's pattern wants `p</a>` straight after the quality, so it matches
# nothing and the video fails with "No video formats found!". The links
# themselves still work from any address: the page mints their token for
# whoever loaded it.
_R34_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref="(?P<url>[^"]+download=true[^"]*)"[^>]*>(?P<label>.*?)</a>',
    re.S)
# 'MP4 2160p 125 MB' — the 360 link has been seen without its 'p'.
_R34_LABEL_RE = re.compile(r'(?P<ext>[A-Za-z0-9]+)\s+(?P<height>\d+)p?(?=\s|$)')
_SIZE_RE = re.compile(r'(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT])i?B\b', re.I)
_SIZE_UNITS = {'K': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12}


def _text(fragment):
    """Visible text of an HTML fragment. Tags become spaces, not nothing —
    '2160p<span>125 MB' must not read as '2160p125 MB'."""
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', fragment)).split())


def _approx_size(text):
    m = _SIZE_RE.search(text)
    if not m:
        return None
    return int(float(m.group('num')) * _SIZE_UNITS[m.group('unit').upper()])


def rule34video_formats(webpage):
    """yt-dlp format dicts for a rule34video page's download menu, old or
    new markup. Empty when the page has no such menu."""
    formats = []
    for link in _R34_LINK_RE.finditer(webpage or ''):
        label = _text(link.group('label'))
        m = _R34_LABEL_RE.search(label)
        if not m:
            continue
        height = int(m.group('height'))
        formats.append({
            'url': html.unescape(link.group('url')),
            'format_id': f'{height}p',
            'ext': m.group('ext').lower(),
            'height': height,
            'quality': height,
            'filesize_approx': _approx_size(label[m.end():]),
        })
    return formats


def _rule34video_real_extract(original):
    """The built-in _real_extract, plus the download menu when it found no
    formats. Its metadata (title, tags, duration…) is kept either way."""
    def _real_extract(self, url):
        info = original(self, url)
        if info.get('formats'):
            return info
        video_id = info.get('id') or self._match_id(url)
        # A second fetch, but only on the broken path — and the tokens it
        # carries are fresh, minted for this address like the first's.
        webpage = self._download_webpage(
            url, video_id, note='Reading the download menu')
        info['formats'] = rule34video_formats(webpage)
        logging.info("ytdlp_fixes: rule34video stepped in for %s — %d "
                     "format(s) from the download menu (yt-dlp#17792)",
                     video_id, len(info['formats']))
        return info
    _real_extract.stopgap_for = original
    return _real_extract


def _patch(cls, name, wrap):
    current = getattr(cls, name)
    if getattr(current, 'stopgap_for', None) is not None:
        return          # already applied — the resolver may be re-imported
    setattr(cls, name, wrap(current))


def apply():
    """Install every stopgap whose extractor exists. Patching the real
    class is enough: yt-dlp's lazy extractor stubs build their instances
    from it."""
    try:
        from yt_dlp.extractor.rule34video import Rule34VideoIE
    except Exception:       # not installed, or the extractor is gone
        return
    _patch(Rule34VideoIE, '_real_extract', _rule34video_real_extract)
