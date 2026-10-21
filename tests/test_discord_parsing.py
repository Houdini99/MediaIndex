"""discord_bot/parsing.py — message parsing and human-readable formatting."""
import pytest

from discord_bot import parsing


# ── Message-text → tags heuristic ──────────────────────────────────────────
def test_message_tags_comma_list():
    assert parsing.parse_message_tags('tag1,tag2,tag3') == ['tag1', 'tag2', 'tag3']


def test_message_tags_trimmed_and_empties_dropped():
    assert parsing.parse_message_tags(' cats ,  black cat ,, ') == ['cats', 'black cat']


def test_message_tags_single_word():
    assert parsing.parse_message_tags('cats') == ['cats']


def test_message_tags_rejects_plain_comment():
    assert parsing.parse_message_tags('look at this lol') == []


def test_message_tags_rejects_links_mentions_emoji():
    assert parsing.parse_message_tags('cats, https://example.com') == []
    assert parsing.parse_message_tags('<@111111111111111111>, cats') == []
    assert parsing.parse_message_tags('<:pepe:1234>, memes') == []


def test_message_tags_rejects_multiline_and_oversize():
    assert parsing.parse_message_tags('cats,\ndogs') == []
    assert parsing.parse_message_tags('x' * 201) == []
    assert parsing.parse_message_tags('a,' * 25 + 'b') == []          # > 20 tags
    assert parsing.parse_message_tags('cats,' + 'y' * 51) == []       # tag too long


def test_message_tags_empty_content():
    assert parsing.parse_message_tags('') == []
    assert parsing.parse_message_tags(None) == []
    assert parsing.parse_message_tags('   ') == []


# ── URL extraction / direct-media detection ────────────────────────────────
ALLOWED = {'jpg', 'jpeg', 'png', 'webp', 'gif', 'mp4', 'webm', 'avi', 'mov', 'mkv'}


def test_extract_urls_basic_and_multiple():
    assert parsing.extract_urls('see https://a.com/x.gif and http://b.de/y') == \
        ['https://a.com/x.gif', 'http://b.de/y']


def test_extract_urls_unwraps_and_trims():
    assert parsing.extract_urls('<https://a.com/x.gif>') == ['https://a.com/x.gif']
    assert parsing.extract_urls('(https://a.com/x.png), done.') == ['https://a.com/x.png']
    assert parsing.extract_urls('no links here') == []
    assert parsing.extract_urls(None) == []


_R34_FILE = ('https://rule34video.com/get_file/51/0123456789abcdef0123456789abcdef'
             '/1234000/1234567/1234567_2160p.mp4/?v-acctoken=AAA'
             '&download=true&download_filename='
             'some-video-4k60fps_2160p.mp4')


def test_a_signed_rule34video_download_link_becomes_its_page():
    """The link's token was minted for whoever copied it, so from the
    server it is a 403; the page mints a fresh one for the server."""
    assert parsing.extract_urls(f'look {_R34_FILE} nice') == [
        'https://rule34video.com/video/1234567/some-video-4k60fps/']


def test_a_signed_link_without_a_filename_still_reaches_the_page():
    """The player's own links carry no download_filename. Any slug
    reaches the page — the site redirects to the real one."""
    url = ('https://www.rule34video.com/get_file/51/abcdef0123456789abcd'
           '/1234000/1234568/1234568_360.mp4/?v-acctoken=BBB')
    assert parsing.extract_urls(url) == [
        'https://rule34video.com/video/1234568/video/']


@pytest.mark.parametrize('url', [
    'https://rule34video.com/video/1234567/some-slug/',      # already a page
    'https://rule34video.com/get_file/oops/',
    'https://evil.example/get_file/51/abc/1234000/1234567/x.mp4/',
    'https://notrule34video.com/get_file/51/abc/1234000/1234567/x.mp4/',
])
def test_other_links_are_left_alone(url):
    assert parsing.extract_urls(url) == [url]


def test_strip_urls_leaves_tag_text():
    assert parsing.strip_urls('cats,funny https://tenor.com/view/x-123') == 'cats,funny'
    assert parsing.strip_urls('<https://a.com/x.gif>') == ''
    assert parsing.strip_urls('plain text') == 'plain text'


def test_media_url_info_path_extension():
    assert parsing.media_url_info('https://cdn.x.com/funny.gif', ALLOWED) == 'funny.gif'
    assert parsing.media_url_info('https://x.com/a/CAT.MP4?ex=123&sig=y', ALLOWED) == 'CAT.MP4'
    assert parsing.media_url_info('https://x.com/sp%20ace.png', ALLOWED) == 'sp ace.png'


def test_media_url_info_twitter_format_param():
    assert parsing.media_url_info(
        'https://pbs.twimg.com/media/abc?format=jpg&name=large', ALLOWED) == 'abc.jpg'


def test_media_url_info_rejects_pages_and_other_schemes():
    assert parsing.media_url_info('https://tenor.com/view/funny-cat-123', ALLOWED) is None
    assert parsing.media_url_info('https://www.youtube.com/watch?v=x', ALLOWED) is None
    assert parsing.media_url_info('https://x.com/evil.exe', ALLOWED) is None
    assert parsing.media_url_info('ftp://x.com/a.gif', ALLOWED) is None


# ── Discord CDN signed-URL helpers ─────────────────────────────────────────
def test_is_discord_cdn():
    assert parsing.is_discord_cdn(
        'https://cdn.discordapp.com/attachments/1/2/a.gif?ex=1&is=2&hm=3')
    assert parsing.is_discord_cdn('https://media.discordapp.net/attachments/1/2/a.png')
    assert not parsing.is_discord_cdn('https://media.tenor.com/abc/x.mp4')
    assert not parsing.is_discord_cdn('https://cdn.example.com/a.gif')
    assert not parsing.is_discord_cdn('not a url')


# ── Retry-delay formatting ─────────────────────────────────────────────────
def test_format_duration_picks_two_units():
    assert parsing.format_duration(0) == '0s'
    assert parsing.format_duration(45) == '45s'
    assert parsing.format_duration(60) == '1m'
    assert parsing.format_duration(150) == '2m 30s'
    assert parsing.format_duration(3600) == '1h'
    assert parsing.format_duration(5400) == '1h 30m'
    # seconds are dropped once hours are in play — this labels a retry
    # delay, not a stopwatch
    assert parsing.format_duration(5445) == '1h 30m'


def test_format_duration_clamps_negatives():
    assert parsing.format_duration(-10) == '0s'


# ── Announcement tag links ─────────────────────────────────────────────────
BASE = 'https://media.example.com'


def test_format_tag_links_builds_search_links():
    out = parsing.format_tag_links('cats,funny', BASE)
    assert out == (f'[cats]({BASE}/?search=cats)'
                   f' · [funny]({BASE}/?search=funny)')


def test_format_tag_links_drops_user_tags_blanks_and_dupes():
    out = parsing.format_tag_links('cats,,user:alice, cats ', BASE)
    assert out == f'[cats]({BASE}/?search=cats)'
    assert parsing.format_tag_links('user:alice', BASE) == ''
    assert parsing.format_tag_links('', BASE) == ''
    assert parsing.format_tag_links(None, BASE) == ''


def test_format_tag_links_urlencodes_and_escapes():
    out = parsing.format_tag_links('two words,a[b]c', BASE)
    assert f'[two words]({BASE}/?search=two%20words)' in out
    assert f'[abc]({BASE}/?search=a%5Bb%5Dc)' in out


def test_format_tag_links_respects_limit():
    tags = ','.join(f'tag{i:02d}' for i in range(100))
    out = parsing.format_tag_links(tags, BASE, limit=200)
    assert len(out) <= 200
    assert out.endswith('…')
    assert out.count('](') >= 2   # something useful survived the cut


# ── Download progress lines ────────────────────────────────────────────────
def test_format_progress_all_fields():
    out = parsing.format_progress(12_300_000, 45_600_000, 3_200_000, 11)
    assert out == '12.3/45.6 MB (27%) · 3.2 MB/s · ETA 11s'


def test_format_progress_omits_unknown_fields():
    assert parsing.format_progress(5_000_000, None, 1_000_000, None) == \
        '5.0 MB · 1.0 MB/s'
    assert parsing.format_progress(1_000_000) == '1.0 MB'


def test_format_progress_eta_minutes():
    out = parsing.format_progress(1_000_000, 100_000_000, None, 125)
    assert out.endswith('ETA 2m05s')


# ── `link CODE` DMs ────────────────────────────────────────────────────────
@pytest.mark.parametrize('text, code', [
    ('link K7QX-M2PA', 'K7QX-M2PA'),
    ('  !link k7qxm2pa ', 'k7qxm2pa'),          # case, no dash, ! habit
    ('/LINK K7QX-M2PA', 'K7QX-M2PA'),
    ('link', ''),                              # bare: explain, don't ignore
])
def test_link_command(text, code):
    assert parsing.parse_link_command(text) == code


@pytest.mark.parametrize('text', [
    'link https://youtu.be/abc',    # a DM about a link must still be ingested
    'link to my video',
    'link K7QX-M2PA please',
    'hello',
    '',
    None,
])
def test_not_a_link_command(text):
    assert parsing.parse_link_command(text) is None
