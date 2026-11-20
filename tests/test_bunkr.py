"""bots_common/bunkr.py: a Bunkr link is a page, so its file is asked for.

The bug this pins: `https://bunkr.cr/f/<name>.mp4` ends in `.mp4`, so both
bots took it for a direct file link and indexed Bunkr's 40 KB viewer page
as a "video". The file is on Bunkr's CDN behind a short-lived token, and
the only way to it is the one the page's Download button takes — the
page names the file's id and a signing service, Bunkr's API names the
file's CDN path, the signing service puts a token on it.

The page snippets below are cut down from the real pages (2026-11), keeping
only what the bot reads. Every request is faked and so is DNS, so nothing
here opens a socket — and every hop is still put through require_public,
because the API host and the signing service come from a page the sender
chose.
"""
import asyncio
import json
import urllib.parse

import pytest

from bots_common import bunkr, download, sniff
from bots_common.parsing import ALLOWED_EXT
from test_download import FakeResponse, FakeSession

PAGE = 'https://bunkr.cr/f/beach-day_source-Ab12Cd34.mp4'
BARE_PAGE = 'https://bunkr.cr/f/Xy7AbCdEfGh2K'
DL_PAGE = 'https://dl.bunkr.cr/file/1234567'
API = 'https://dl.bunkr.cr/api/_001_v2'
SIGN = 'https://glb-apisign.cdn.cr/sign'
CDN = 'https://mlk-b.cdn.cr/storage/media/beach-day_source-Ab12Cd34.mp4'

FILE_PAGE_HTML = '''<html><head>
<title>beach-day_source.mp4 | Bunkr</title></head>
<div id="fileTracker" data-file-id="1234567" style="display: none;"></div>
<a class="btn btn-main" href="https://dl.bunkr.cr/file/1234567" target="_blank">Download</a>
<script type="text/javascript">
    var jsCDN         = "https:\\/\\/mlk-b.cdn.cr\\/storage\\/media\\/beach-day_source-Ab12Cd34.mp4";
    var signUrl       = "https://glb-apisign.cdn.cr/sign";
</script></html>'''

DL_PAGE_HTML = '''<html><head>
<title>Download beach-day_source.mp4</title>
<script>var ogname = "beach-day_source.mp4";</script>
<script>
const SIGN_SERVICE_URL = 'https://glb-apisign.cdn.cr/sign';
</script></head>
<a id="download-btn" href="#" data-id="1234567">Download</a></html>'''

API_ANSWER = {'mediafiles': 'https://mlk-b.cdn.cr',
              'original': 'beach-day_source.mp4',
              'path': '/storage/media/beach-day_source-Ab12Cd34.mp4'}
SIGN_ANSWER = {'ex': 1795000000,
               'token': '0123456789abcdef0123456789abcdef01234567'}
SIGNED = f'{CDN}?token={SIGN_ANSWER["token"]}&ex={SIGN_ANSWER["ex"]}'


def run(coro):
    return asyncio.run(coro)


def page(html, status=200):
    return FakeResponse(status, chunks=[html.encode()],
                        headers={'Content-Type': 'text/html'})


def answer(payload):
    return FakeResponse(chunks=[json.dumps(payload).encode()],
                        headers={'Content-Type': 'application/json'})


def video(data=b'\x00\x00\x00\x18ftypmp42'):
    return FakeResponse(chunks=[data],
                        headers={'Content-Type': 'application/octet-stream'})


ADDRESSES = {'bunkr.cr': ['104.21.1.1'], 'dl.bunkr.cr': ['104.21.1.2'],
             'bunkr.si': ['104.21.1.5'],
             'glb-apisign.cdn.cr': ['104.21.1.3'],
             'mlk-b.cdn.cr': ['95.216.1.4'], 'web': ['172.18.0.5'],
             'files.example': ['93.184.216.34']}


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    def getaddrinfo(host, port, **kw):
        return [(0, 0, 0, '', (address, port)) for address in ADDRESSES[host]]
    monkeypatch.setattr(sniff.socket, 'getaddrinfo', getaddrinfo)


# ── Which links are Bunkr files ───────────────────────────────────────────
@pytest.mark.parametrize('url', [
    PAGE, BARE_PAGE, DL_PAGE,
    'https://bunkr.cr/v/beach-day_source-Ab12Cd34.mp4',
    'https://bunkr.cr/i/picture-AbCdEf12.jpg',
    'https://bunkr.si/f/Xy7AbCdEfGh2K/',
    'https://bunkrr.su/f/Xy7AbCdEfGh2K',
    'https://www.bunkr.la/f/Xy7AbCdEfGh2K',
    'https://get.bunkrr.su/file/1234567',
    'http://BUNKR.CR/f/Xy7AbCdEfGh2K',
])
def test_a_link_to_one_file_on_any_bunkr_domain_is_one(url):
    assert bunkr.is_file_link(url)


@pytest.mark.parametrize('url', [
    'https://bunkr.cr/a/eoKRLC2Q',             # an album: many files
    'https://bunkr.cr/',
    'https://bunkr.cr/f/',
    'https://bunkr.cr/f/a/b',
    'https://dl.bunkr.cr/file/abc',
    'https://bunkr.cr.evil.example/f/Xy7AbCdEfGh2K',
    'https://notbunkr.cr/f/Xy7AbCdEfGh2K',
    'https://example.com/f/Xy7AbCdEfGh2K',
    CDN,                                       # the file itself, not a page
    'ftp://bunkr.cr/f/Xy7AbCdEfGh2K',
    'not a url',
])
def test_anything_else_is_not(url):
    assert not bunkr.is_file_link(url)


# ── Reading the page and the answers ──────────────────────────────────────
def test_a_file_page_names_its_download_page_and_signing_service():
    assert bunkr.page_refs(FILE_PAGE_HTML, PAGE) == (API, '1234567', SIGN)


def test_a_download_page_names_its_file_in_its_own_url():
    assert bunkr.page_refs(DL_PAGE_HTML, DL_PAGE) == (API, '1234567', SIGN)


def test_an_escaped_signing_url_is_unescaped():
    html = FILE_PAGE_HTML.replace(
        '"https://glb-apisign.cdn.cr/sign"',
        '"https:\\/\\/glb-apisign.cdn.cr\\/sign"')
    assert bunkr.page_refs(html, PAGE)[2] == SIGN


@pytest.mark.parametrize('html, says', [
    ('<html>an album, or a 404 page</html>', 'no file on that Bunkr page'),
    (FILE_PAGE_HTML.replace('signUrl', 'somethingElse'), 'has changed'),
    (FILE_PAGE_HTML.replace('"https://glb-apisign', '"http://glb-apisign'),
     'not an https link'),
])
def test_a_page_without_what_the_button_needs_says_so(html, says):
    with pytest.raises(bunkr.Unreadable, match=says):
        bunkr.page_refs(html, PAGE)


def test_the_api_answer_names_the_cdn_file_and_its_original_name():
    assert bunkr.api_file(API_ANSWER) == (
        CDN, 'beach-day_source.mp4')


def test_without_an_original_name_the_cdn_name_stands_in():
    payload = dict(API_ANSWER, original='')
    assert bunkr.api_file(payload)[1] == \
        'beach-day_source-Ab12Cd34.mp4'


@pytest.mark.parametrize('payload', [
    None, [], {}, {'encrypted': True, 'url': 'b64…'},
    dict(API_ANSWER, mediafiles='http://mlk-b.cdn.cr'),
    dict(API_ANSWER, path='storage/media/x.mp4'),
])
def test_an_api_answer_of_another_shape_is_refused(payload):
    with pytest.raises(bunkr.Unreadable, match='download API'):
        bunkr.api_file(payload)


@pytest.mark.parametrize('payload', [None, {}, {'token': 'x'}, {'ex': 1}])
def test_a_signing_answer_without_a_token_is_refused(payload):
    with pytest.raises(bunkr.Unreadable, match='signing service'):
        bunkr.signed(CDN, payload)


# ── The whole walk, as download.fetch takes it ────────────────────────────
def test_a_bunkr_link_is_fetched_as_its_signed_file(tmp_path):
    """Page, API, signing service, then the file — never the page itself
    written to disk under the video's name."""
    dst = tmp_path / 'out.mp4'
    session = FakeSession(
        get=[page(FILE_PAGE_HTML), answer(SIGN_ANSWER), video(b'movie')],
        post=[answer(API_ANSWER)])

    run(download.fetch(session, PAGE, str(dst), 'clip.mp4'))

    assert dst.read_bytes() == b'movie'
    sign_request = session.gets[1]
    assert session.gets == [PAGE, sign_request, SIGNED]
    assert sign_request == (f'{SIGN}?path=' + urllib.parse.quote(
        '/storage/media/beach-day_source-Ab12Cd34.mp4', safe=''))
    assert session.posts == [(API, session.posts[0][1])]
    assert session.posts[0][1]['json'] == {'id': '1234567'}
    assert all(kw['allow_redirects'] is False for kw in session.get_kwargs)


def test_the_download_pages_link_takes_the_same_walk(tmp_path):
    dst = tmp_path / 'out.mp4'
    session = FakeSession(
        get=[page(DL_PAGE_HTML), answer(SIGN_ANSWER), video(b'movie')],
        post=[answer(API_ANSWER)])

    run(download.fetch(session, DL_PAGE, str(dst), 'clip.mp4'))

    assert dst.read_bytes() == b'movie'
    assert session.gets[-1] == SIGNED


def test_an_old_style_link_follows_its_redirect_to_the_file_page(tmp_path):
    old = 'https://bunkr.cr/v/beach-day_source-Ab12Cd34.mp4'
    session = FakeSession(
        get=[FakeResponse(301, headers={'Location': PAGE}),
             page(FILE_PAGE_HTML), answer(SIGN_ANSWER), video()],
        post=[answer(API_ANSWER)])

    run(download.fetch(session, old, str(tmp_path / 'out.mp4'), 'clip.mp4'))

    assert session.gets[:2] == [old, PAGE]


def test_a_signing_service_inside_the_stack_is_never_asked(tmp_path):
    """The page is the sender's choice, so what it names is too."""
    html = FILE_PAGE_HTML.replace(SIGN, 'https://web/sign')
    session = FakeSession(get=[page(html)], post=[answer(API_ANSWER)])

    with pytest.raises(sniff.Blocked):
        run(download.fetch(session, PAGE, str(tmp_path / 'x'), 'clip.mp4'))

    assert session.gets == [PAGE]


def test_a_cdn_host_inside_the_stack_is_never_fetched(tmp_path):
    session = FakeSession(
        get=[page(FILE_PAGE_HTML), answer(SIGN_ANSWER)],
        post=[answer(dict(API_ANSWER, mediafiles='https://web'))])

    with pytest.raises(sniff.Blocked):
        run(download.fetch(session, PAGE, str(tmp_path / 'x'), 'clip.mp4'))

    assert len(session.gets) == 2          # page and signing, not the file


def test_a_page_with_no_file_on_it_is_not_a_file(tmp_path):
    dst = tmp_path / 'out.mp4'
    session = FakeSession(get=[page('<html>File not found</html>')])

    with pytest.raises(download.NotAFile, match='no file on that Bunkr page'):
        run(download.fetch(session, PAGE, str(dst), 'clip.mp4'))

    assert not dst.exists()


def test_an_api_that_does_not_answer_json_is_not_a_file(tmp_path):
    session = FakeSession(get=[page(FILE_PAGE_HTML)],
                          post=[page('<html>Just a moment…</html>')])

    with pytest.raises(download.NotAFile, match='did not answer with JSON'):
        run(download.fetch(session, PAGE, str(tmp_path / 'x'), 'clip.mp4'))


def test_an_error_from_bunkr_is_an_http_error_like_any_other(tmp_path):
    """A 5xx from Bunkr is the site having a bad moment, which the
    Discord bot retries — so it surfaces exactly as a CDN's would."""
    session = FakeSession(get=[page('', status=503)])

    with pytest.raises(sniff.aiohttp.ClientResponseError):
        run(download.fetch(session, PAGE, str(tmp_path / 'x'), 'clip.mp4'))


def test_an_oversized_page_is_not_read_to_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(bunkr, 'MAX_PAGE_BYTES', 100)
    session = FakeSession(get=[FakeResponse(chunks=[b'x' * 60] * 50)])

    with pytest.raises(download.NotAFile, match='far too big'):
        run(download.fetch(session, PAGE, str(tmp_path / 'x'), 'clip.mp4'))


# ── Naming a link that names no file ──────────────────────────────────────
def test_a_bare_link_is_named_by_the_api_and_kept_as_the_link():
    """No token is minted here: a parked or retried link would outlive it.
    download.fetch signs it when it is actually fetched."""
    session = FakeSession(get=[page(FILE_PAGE_HTML)],
                          post=[answer(dict(API_ANSWER,
                                            original='holiday.mp4'))])

    assert run(bunkr.sniff_link(session, BARE_PAGE, ALLOWED_EXT)) == (
        BARE_PAGE, 'holiday.mp4')
    assert session.gets == [BARE_PAGE]     # no signing request


def test_a_bunkr_file_the_index_does_not_take_is_not_named():
    session = FakeSession(get=[page(FILE_PAGE_HTML)],
                          post=[answer(dict(API_ANSWER,
                                            original='archive.zip'))])
    assert run(bunkr.sniff_link(session, BARE_PAGE, ALLOWED_EXT)) is None


@pytest.mark.parametrize('get', [
    page('<html>gone</html>'),
    page('', status=404),
    asyncio.TimeoutError(),
    sniff.aiohttp.ClientError(),
])
def test_a_failed_lookup_is_just_not_media(get):
    """Never raises — the bot's other paths still get their turn."""
    class Raising(FakeSession):
        def get(self, url, **kw):
            if isinstance(get, BaseException):
                raise get
            return super().get(url, **kw)
    session = Raising(get=[get])
    assert run(bunkr.sniff_link(session, BARE_PAGE, ALLOWED_EXT)) is None


def test_the_shared_sniff_asks_bunkr_its_own_way_and_keeps_the_order():
    """One HEAD for an ordinary link, the API for a Bunkr one, the answers
    in the order the links were sent."""
    bare = 'https://files.example/ab/cd/abcdef0123456789abcdef0123456789'

    class Session(FakeSession):
        def head(self, url, **kw):
            self.heads = getattr(self, 'heads', []) + [url]
            return FakeResponse(headers={'Content-Type': 'video/mp4'})
    session = Session(get=[page(FILE_PAGE_HTML)], post=[answer(API_ANSWER)])
    d = download.Downloader()
    d._session = session

    found = run(d.sniff_links([BARE_PAGE, bare], ALLOWED_EXT))

    assert list(found) == [BARE_PAGE, bare]
    assert found[BARE_PAGE] == (BARE_PAGE, 'beach-day_source.mp4')
    assert session.heads == [bare]


def test_the_per_message_cap_counts_bunkr_links_too():
    d = download.Downloader()
    d._session = FakeSession()
    asked = []

    async def fake(session, url, allowed_ext):
        asked.append(url)
    links = [f'https://bunkr.cr/f/file{n}' for n in range(10)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bunkr, 'sniff_link', fake)
        run(d.sniff_links(links, ALLOWED_EXT))
    assert len(asked) == sniff.MAX_PER_MESSAGE


# ── Albums ────────────────────────────────────────────────────────────────
ALBUM = 'https://bunkr.cr/a/Qw3rTy12'
ADVANCED = 'https://bunkr.cr/a/Qw3rTy12?advanced=1'
PHOTOS = [(f'IMG_{n}.jpg', f'IMG_{n}-Ab{n}.jpg') for n in range(5)]


def album_page(entries, title='Holiday 2022'):
    """An album's advanced view, cut down to what the bot reads — its
    title and window.albumFiles, written the way Bunkr writes them
    (2026-11): a JS array of object literals with unquoted keys."""
    body = ',\n'.join(
        '{\n  id:  %d ,\n  name: %s,\n  original: %s,\n  slug: %s,\n'
        '  type: "image/jpeg",\n  size:  %d ,\n  cdnEndpoint: %s\n}'
        % (1000000 + n, json.dumps(slug), json.dumps(original),
           json.dumps(slug), 1000 + n, json.dumps('/' + slug))
        for n, (original, slug) in enumerate(entries))
    return (f'<html><head><title>{title} | Bunkr</title></head><script>\n'
            f'window.albumFiles = [\n{body}\n];\n'
            f"console.log('Album files embedded:', "
            f"window.albumFiles.length, 'files');\n</script></html>")


def read(session, url=ALBUM, limit=20):
    return run(bunkr.read_album(session, url, ALLOWED_EXT, limit))


@pytest.mark.parametrize('url, expected', [
    (ALBUM, True),
    ('https://bunkr.si/a/Qw3rTy12/', True),
    ('https://bunkrr.su/a/Qw3rTy12', True),
    (PAGE, False),
    ('https://bunkr.cr/a/', False),
    ('https://bunkr.cr/a/Qw3rTy12/extra', False),
    ('https://example.com/a/Qw3rTy12', False),
])
def test_an_album_link_is_told_apart_from_a_file_link(url, expected):
    assert bunkr.is_album_link(url) is expected
    if expected:
        assert not bunkr.is_file_link(url)


def test_the_advanced_view_lists_every_file_with_its_fields():
    entries = bunkr.album_entries(album_page(PHOTOS[:2]))
    assert [(e['original'], e['slug'], e['size']) for e in entries] == [
        ('IMG_0.jpg', 'IMG_0-Ab0.jpg', '1000'),
        ('IMG_1.jpg', 'IMG_1-Ab1.jpg', '1001')]


def test_a_name_with_braces_and_quotes_does_not_end_its_entry_early():
    odd = [('a {weird} "name".jpg', 'a-weird-name-Xy.jpg'),
           ('café.mp4', 'cafe-Zz.mp4')]
    entries = bunkr.album_entries(album_page(odd))
    assert [e['original'] for e in entries] == ['a {weird} "name".jpg',
                                                'café.mp4']


def test_a_page_without_the_file_list_is_unreadable():
    with pytest.raises(bunkr.Unreadable, match='list of files'):
        bunkr.album_entries('<html>an album, but laid out anew</html>')


def test_the_album_title_loses_the_sites_suffix():
    assert bunkr.album_title(
        '<title>Cats &amp; Dogs | Bunkr</title>') == 'Cats & Dogs'
    assert bunkr.album_title('<html></html>') == ''


def test_an_album_is_read_in_one_request_into_file_links_in_order():
    session = FakeSession(get=[page(album_page(PHOTOS))])

    album = read(session)

    assert session.gets == [ADVANCED]
    assert album.files == [(f'https://bunkr.cr/f/{slug}', original)
                           for original, slug in PHOTOS]
    assert all(bunkr.is_file_link(link) for link, _ in album.files)
    assert album.title == 'Holiday 2022' and album.total == 5
    assert album.note() is None and album.reason is None


def test_only_the_first_files_up_to_the_limit_are_taken_and_that_is_said():
    album = read(FakeSession(get=[page(album_page(PHOTOS))]), limit=2)

    assert [name for _, name in album.files] == ['IMG_0.jpg', 'IMG_1.jpg']
    assert album.over_limit == 3
    assert album.note() == ('taking the first 2 of the 5 images and videos '
                            'in “Holiday 2022” (the album limit)')


def test_files_the_index_does_not_take_are_skipped_and_counted():
    mixed = [('notes.txt', 'notes-Aa.txt'), ('clip.MP4', 'clip-Bb.MP4'),
             ('archive.zip', 'archive-Cc.zip')]
    album = read(FakeSession(get=[page(album_page(mixed))]))

    assert album.files == [('https://bunkr.cr/f/clip-Bb.MP4', 'clip.MP4')]
    assert album.unsupported == 2
    assert album.note() == ("skipping 2 files in “Holiday 2022” that the "
                            "index doesn't take")


def test_an_album_of_nothing_the_index_takes_says_so():
    album = read(FakeSession(get=[page(album_page([('a.zip', 'a-Q.zip')]))]))
    assert album.files == []
    assert album.note() == ("“Holiday 2022” has no images or videos the "
                            "index takes")


def test_an_empty_album_says_so():
    album = read(FakeSession(get=[page(album_page([]))]))
    assert album.files == [] and album.note() == '“Holiday 2022” is empty'


def test_a_slug_is_quoted_into_its_file_link():
    album = read(FakeSession(get=[page(album_page([('a b.jpg',
                                                    'a b#1-Q.jpg')]))]))
    [(link, _)] = album.files
    assert link == 'https://bunkr.cr/f/a%20b%231-Q.jpg'
    assert bunkr.is_file_link(link)


def test_a_name_cannot_smuggle_a_path_into_the_filename():
    album = read(FakeSession(get=[page(album_page([('../../x.jpg',
                                                    'x-Q.jpg')]))]))
    assert album.files[0][1] == '.._.._x.jpg'


def test_the_file_links_follow_the_album_to_the_domain_it_moved_to():
    session = FakeSession(get=[
        FakeResponse(301, headers={'Location': 'https://bunkr.si/a/Qw3rTy12'
                                               '?advanced=1'}),
        page(album_page(PHOTOS[:1]))])
    album = read(session)
    assert album.files[0][0] == 'https://bunkr.si/f/IMG_0-Ab0.jpg'


@pytest.mark.parametrize('response, says', [
    (page('', status=404), 'HTTP 404'),
    (page('<html>Album not found</html>'), 'list of files'),
    (FakeResponse(302, headers={'Location': 'https://web/a/x'}),
     'not a public address'),
])
def test_an_album_that_cannot_be_read_says_why_and_never_raises(response,
                                                                says):
    album = read(FakeSession(get=[response]))
    assert album.files == [] and says in album.note()


def test_an_oversized_album_page_is_not_read_to_the_end(monkeypatch):
    monkeypatch.setattr(bunkr, 'MAX_ALBUM_BYTES', 100)
    album = read(FakeSession(get=[FakeResponse(chunks=[b'x' * 60] * 50)]))
    assert 'far too big' in album.note()


def test_the_downloader_reads_only_album_links_and_only_a_few():
    d = download.Downloader()
    d._session = FakeSession()
    asked = []

    async def fake(session, url, allowed_ext, limit):
        asked.append((url, limit))
        return bunkr.Album(url)
    albums = [f'https://bunkr.cr/a/album{n}' for n in range(10)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bunkr, 'read_album', fake)
        found = run(d.read_albums([PAGE, *albums, albums[0]], ALLOWED_EXT, 7))
    assert list(found) == albums[:sniff.MAX_PER_MESSAGE]
    assert asked == [(url, 7) for url in albums[:sniff.MAX_PER_MESSAGE]]


def test_no_album_links_means_no_session_at_all():
    d = download.Downloader()
    assert run(d.read_albums([PAGE, BARE_PAGE], ALLOWED_EXT, 20)) == {}
    assert d._session is None


# ── Albums in the Discord bot ─────────────────────────────────────────────
class _Channel:
    """Records the embed re-fetch the bot makes for a link nothing came
    of — which a settled album must not need."""
    id = 100

    def __init__(self):
        self.refetched = 0

    async def fetch_message(self, message_id):
        import types
        self.refetched += 1
        return types.SimpleNamespace(embeds=[])


def _discord_message(content):
    import types
    return types.SimpleNamespace(content=content, attachments=[], embeds=[],
                                 message_snapshots=[], id=200,
                                 channel=_Channel())


@pytest.fixture
def discord_bot(monkeypatch):
    from discord_bot import bot as botmod
    from discord_bot import config as discord_config
    from discord_bot import download as discord_download

    b = botmod.IndexBot.__new__(botmod.IndexBot)
    b._downloader = discord_download.CdnDownloader()
    b._downloader._session = 'session'          # never opened
    b.albums, b.album_reads, b.reports, b.reactions = {}, [], [], []

    async def read_albums(urls, allowed_ext, limit):
        b.album_reads.append((list(urls), limit))
        return {url: b.albums[url] for url in urls if url in b.albums}

    async def report(message, text):
        b.reports.append(text)

    async def react(message, emoji):
        b.reactions.append(emoji)
    async def head_sniff(session, url, allowed_ext):
        b.sniffed.append(url)
    b.sniffed = []
    b._downloader.read_albums = read_albums
    b._report, b._react = report, react
    monkeypatch.setattr(botmod.sniff, 'sniff', head_sniff)
    monkeypatch.setattr(discord_config, 'EMBED_UNFURL_WAIT', 0)
    monkeypatch.setattr(discord_config, 'ALBUM_MAX_FILES', 20)
    monkeypatch.setattr(botmod.IndexBot, '_resolver_enabled',
                        staticmethod(lambda: False))
    b.botmod, b.config = botmod, discord_config
    return b


def _two_files(**kw):
    return bunkr.Album(ALBUM, 'Holiday 2022',
                       [('https://bunkr.cr/f/a-Q.jpg', 'a.jpg'),
                        ('https://bunkr.cr/f/b-Q.mp4', 'b.mp4')], 2, **kw)


def test_discord_ingests_an_albums_files_like_links_in_the_message(
        discord_bot):
    discord_bot.albums[ALBUM] = _two_files()

    sources, tag_text, urls = run(discord_bot._gather_sources(
        _discord_message(f'cats,beach {ALBUM}')))

    assert [(s.url, s.filename) for s in sources] == [
        ('https://bunkr.cr/f/a-Q.jpg', 'a.jpg'),
        ('https://bunkr.cr/f/b-Q.mp4', 'b.mp4')]
    assert tag_text == 'cats,beach'
    assert urls == [], "an album is settled here, never handed to yt-dlp"
    assert discord_bot.album_reads == [([ALBUM], 20)]
    assert discord_bot.reports == [] and discord_bot.reactions == []


def test_discord_leaves_an_albums_note_to_its_status_message(discord_bot):
    """What an album left out goes at the top of the album's one status
    message (album_status.py), so gathering says nothing yet — and each
    file carries its album there."""
    alb = discord_bot.albums[ALBUM] = _two_files(over_limit=40)
    sources, _, _ = run(discord_bot._gather_sources(_discord_message(ALBUM)))
    assert discord_bot.reports == []
    assert all(s.album is alb for s in sources)


def test_discord_answers_an_album_nothing_comes_of_on_the_spot(discord_bot):
    discord_bot.albums[ALBUM] = bunkr.Album(
        ALBUM, reason='Bunkr answered HTTP 404 for that album')

    message = _discord_message(ALBUM)
    sources, _, urls = run(discord_bot._gather_sources(message))

    assert sources == [] and urls == []
    assert discord_bot.reports == [
        f'🚫 <{ALBUM}> — Bunkr answered HTTP 404 for that album']
    assert discord_bot.reactions == [discord_bot.botmod.UNSUPPORTED]
    assert message.channel.refetched == 0


def test_discord_albums_can_be_turned_off(discord_bot, monkeypatch):
    monkeypatch.setattr(discord_bot.config, 'ALBUM_MAX_FILES', 0)
    discord_bot.albums[ALBUM] = _two_files()

    sources, _, urls = run(discord_bot._gather_sources(
        _discord_message(ALBUM)))

    assert sources == [] and urls == [ALBUM]
    assert discord_bot.album_reads == []
    assert discord_bot.sniffed == [ALBUM], "back to the ordinary paths"
