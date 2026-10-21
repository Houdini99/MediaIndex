"""bots_common/ytdlp_fixes.py — stopgaps for yt-dlp extractors a site
redesign broke.

What is pinned is the contract that makes a stopgap safe to leave running:
it parses the site's current markup, and it stays out of the way whenever
the built-in extractor found formats itself — so an upstream fix makes it
go quiet rather than fight. The parsing and the wrapper are plain Python,
so a fake extractor stands in for yt-dlp; only the last test needs the
real library.
"""
import pytest

from bots_common import ytdlp_fixes

# The 2026-10 download menu, trimmed: an icon before the label, the size
# after it, and a raw '&' in the href.
NEW_MENU = '''
<div class="fs-sheet__body vp-sheet__body" role="menu">
  <a class="tag_item tag_item_download vp-sheet__item"
     href="https://rule34video.com/get_file/51/ab12/1234000/1234567/1234567_2160p.mp4/?v-acctoken=AAA&download=true&download_filename=x_2160p.mp4"
     data-attach-session="PHPSESSID" role="menuitem"><svg class="vp-ico" viewBox="0 0 24 24"><path d="M11 3h2"/></svg>MP4 2160p<span class="vp-sheet__meta">125 MB</span></a>
  <a class="tag_item tag_item_download vp-sheet__item"
     href="https://rule34video.com/get_file/51/cd34/1234000/1234567/1234567_360.mp4/?v-acctoken=BBB&amp;download=true"
     role="menuitem"><svg class="vp-ico"></svg>MP4 360<span class="vp-sheet__meta">9.5 MB</span></a>
</div>
<a href="https://rule34video.com/video/1234567/other/">Related video</a>
<a href="https://rule34video.com/get_file/x/?download=true">Download</a>
'''

# What yt-dlp's own pattern was written for.
OLD_MENU = ('<a class="tag_item" href="https://rule34video.com/get_file/1/a/2/3/'
            '3_1080p.mp4/?download=true">MP4 1080p</a>')


def test_the_redesigned_download_menu_is_read():
    formats = ytdlp_fixes.rule34video_formats(NEW_MENU)
    assert [(f['format_id'], f['ext'], f['height'], f['filesize_approx'])
            for f in formats] == [('2160p', 'mp4', 2160, 125_000_000),
                                  ('360p', 'mp4', 360, 9_500_000)]
    assert formats[0]['url'].endswith(
        '?v-acctoken=AAA&download=true&download_filename=x_2160p.mp4')
    assert '&amp;' not in formats[1]['url']


def test_the_old_menu_still_reads():
    [fmt] = ytdlp_fixes.rule34video_formats(OLD_MENU)
    assert fmt['height'] == 1080 and fmt['filesize_approx'] is None


def test_a_page_without_a_menu_has_no_formats():
    assert ytdlp_fixes.rule34video_formats('<html>404 Not Found</html>') == []
    assert ytdlp_fixes.rule34video_formats(None) == []


def _builtin(self, url):
    """Stands in for yt-dlp's own _real_extract."""
    return dict(self.builtin)


class FakeExtractor:
    """Just enough InfoExtractor for the wrapper: the built-in result is
    whatever `builtin` says, and every page fetch is recorded."""

    def __init__(self, builtin, page=NEW_MENU):
        self.builtin = builtin
        self.page = page
        self.fetches = []

    def _match_id(self, url):
        return '1234567'

    def _download_webpage(self, url, video_id, note=None):
        self.fetches.append(url)
        return self.page


def _wrapped(ie, url='https://rule34video.com/video/1234567/x/'):
    return ytdlp_fixes._rule34video_real_extract(_builtin)(ie, url)


def test_the_stopgap_fills_in_when_the_builtin_finds_nothing():
    ie = FakeExtractor({'id': '1234567', 'title': 'Clip', 'tags': ['a'],
                        'formats': []})
    info = _wrapped(ie)
    assert [f['height'] for f in info['formats']] == [2160, 360]
    assert info['title'] == 'Clip' and info['tags'] == ['a']   # metadata kept
    assert len(ie.fetches) == 1


def test_the_stopgap_stays_out_of_the_way_once_upstream_works():
    """The day yt-dlp fixes the site, its formats win and no second fetch
    is made — which is what makes leaving the stopgap in place harmless."""
    upstream = [{'url': 'https://u/1.mp4', 'height': 720}]
    ie = FakeExtractor({'id': '1234567', 'formats': upstream})
    assert _wrapped(ie)['formats'] == upstream
    assert ie.fetches == []


def test_patching_twice_wraps_once():
    class IE:
        def _real_extract(self, url):
            return {}

    ytdlp_fixes._patch(IE, '_real_extract', ytdlp_fixes._rule34video_real_extract)
    first = IE._real_extract
    ytdlp_fixes._patch(IE, '_real_extract', ytdlp_fixes._rule34video_real_extract)
    assert IE._real_extract is first
    assert getattr(first.stopgap_for, 'stopgap_for', None) is None


def test_the_real_extractor_is_patched_when_yt_dlp_is_installed():
    pytest.importorskip('yt_dlp')
    from bots_common import resolver      # noqa: F401 — applies the stopgaps
    from yt_dlp.extractor.rule34video import Rule34VideoIE
    assert getattr(Rule34VideoIE._real_extract, 'stopgap_for', None) is not None
