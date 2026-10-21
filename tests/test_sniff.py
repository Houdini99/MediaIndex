"""bots_common/sniff.py, and the Discord bot asking a link what it serves.

The case this exists for: a CDN path like `https://files.example/ab/cd/<md5>`
can be an mp4 — Discord embeds it as one — but the URL names no file, and
a metadata probe answers "no video behind that link". Pinned here:

  * the server's Content-Type and Content-Disposition decide, the way they
    do for Discord's unfurler;
  * every hop of a sniff passes the public-address bar first, because the
    bot sits on the stack's internal networks;
  * only links nothing else will claim are asked at all.

Nothing here opens a socket: DNS and the HTTP session are both faked.
"""
import asyncio
import types

import pytest

from bots_common import sniff
from discord_bot import bot as botmod

ALLOWED = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'mp4', 'webm', 'mov', 'mkv'}
BARE_FILE = 'https://files.example/ab/cd/abcdef0123456789abcdef0123456789'


def run(coro):
    return asyncio.run(coro)


# ── What a response is ────────────────────────────────────────────────────
def test_the_servers_own_filename_wins_and_is_unquoted():
    """Some servers percent-encode inside a plain filename=, and the name
    one offers carries the post's tags — far better than an md5."""
    name = sniff.media_name(BARE_FILE, {
        'Content-Type': 'video/mp4',
        'Content-Disposition': 'inline; filename="1234567 - some_artist'
                               '%20animated%20sound.mp4"'}, ALLOWED)
    assert name == '1234567 - some_artist animated sound.mp4'


def test_without_a_filename_the_type_names_the_extension():
    assert sniff.media_name(BARE_FILE, {'Content-Type': 'video/mp4'},
                            ALLOWED) == 'abcdef0123456789abcdef0123456789.mp4'
    assert sniff.media_name('https://cdn.example/a/', {
        'Content-Type': 'image/jpeg; charset=binary'}, ALLOWED) == 'media.jpg'
    assert sniff.media_name('https://cdn.example/clip', {
        'Content-Type': 'video/quicktime'}, ALLOWED) == 'clip.mov'


@pytest.mark.parametrize('headers', [
    {'Content-Type': 'text/html; charset=utf-8'},
    {'Content-Type': 'application/octet-stream'},          # nothing to go on
    {'Content-Type': 'audio/mpeg'},                         # not taken
    {'Content-Type': 'video/x-flv'},                        # not taken
    {'Content-Type': 'text/html',                           # a page that
     'Content-Disposition': 'attachment; filename="x.mp4"'},  # says otherwise
    {},
])
def test_anything_else_is_not_media(headers):
    assert sniff.media_name(BARE_FILE, headers, ALLOWED) is None


def test_an_octet_stream_is_believed_when_it_names_a_media_file():
    assert sniff.media_name(BARE_FILE, {
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': 'attachment; filename="clip.webm"'},
        ALLOWED) == 'clip.webm'


def test_an_offered_filename_is_never_a_path():
    assert sniff.media_name(BARE_FILE, {
        'Content-Type': 'video/mp4',
        'Content-Disposition': 'attachment; filename="../../etc/x.mp4"'},
        ALLOWED) == 'x.mp4'


def test_an_rfc6266_filename_star_is_decoded():
    assert sniff.media_name(BARE_FILE, {
        'Content-Type': 'video/mp4',
        'Content-Disposition': "attachment; filename*=UTF-8''K%C3%A4se.mp4"},
        ALLOWED) == 'Käse.mp4'


def test_an_offered_name_the_index_wont_take_falls_back_to_the_type():
    assert sniff.media_name(BARE_FILE, {
        'Content-Type': 'video/mp4',
        'Content-Disposition': 'attachment; filename="x.exe"'},
        ALLOWED) == 'abcdef0123456789abcdef0123456789.mp4'


# ── Asking the server ─────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """HEAD only — a sniff must never GET, so there is no get() to call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.asked = []

    def head(self, url, **kw):
        assert kw.get('allow_redirects') is False, \
            "redirects are followed by hand, so each hop is vetted"
        self.asked.append(url)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def dns(monkeypatch):
    table = {'files.example': ['104.21.1.1'],
             'public.example': ['93.184.216.34'],
             'internal.example': ['10.0.0.5']}

    def getaddrinfo(host, port, **kw):
        return [(0, 0, 0, '', (address, port)) for address in table[host]]
    monkeypatch.setattr(sniff.socket, 'getaddrinfo', getaddrinfo)
    return table


def test_a_bare_cdn_path_serving_an_mp4_is_found(dns):
    session = FakeSession([FakeResponse(headers={'Content-Type': 'video/mp4'})])
    assert run(sniff.sniff(session, BARE_FILE, ALLOWED)) == (
        BARE_FILE, 'abcdef0123456789abcdef0123456789.mp4')


def test_redirects_are_followed_to_the_file_and_it_is_fetched_from_there(dns):
    session = FakeSession([
        FakeResponse(302, {'Location': BARE_FILE}),
        FakeResponse(headers={'Content-Type': 'video/mp4'})])
    url, _ = run(sniff.sniff(session, 'https://public.example/v/1', ALLOWED))
    assert url == BARE_FILE
    assert session.asked == ['https://public.example/v/1', BARE_FILE]


def test_a_redirect_into_the_stack_is_never_followed(dns):
    """Hop one is public, hop two is not — and is never requested."""
    session = FakeSession([
        FakeResponse(302, {'Location': 'http://internal.example/x'})])
    assert run(sniff.sniff(session, 'https://public.example/x', ALLOWED)) \
        is None
    assert session.asked == ['https://public.example/x']


def test_an_internal_link_is_not_even_asked(dns):
    session = FakeSession([])
    assert run(sniff.sniff(session, 'http://internal.example/x', ALLOWED)) \
        is None
    assert session.asked == []


def test_a_redirect_loop_gives_up(dns):
    loop = FakeResponse(302, {'Location': 'https://public.example/x'})
    session = FakeSession([loop] * (sniff.MAX_REDIRECTS + 1))
    assert run(sniff.sniff(session, 'https://public.example/x', ALLOWED)) \
        is None


@pytest.mark.parametrize('answer', [
    FakeResponse(404),
    FakeResponse(200, {'Content-Type': 'text/html'}),
    asyncio.TimeoutError(),
    sniff.aiohttp.ClientError(),
])
def test_a_miss_or_a_failure_is_just_not_media(dns, answer):
    """Never raises: the bot's other paths get their turn either way."""
    assert run(sniff.sniff(FakeSession([answer]), BARE_FILE, ALLOWED)) is None


# ── The Discord bot ───────────────────────────────────────────────────────
class FakeChannel:
    def __init__(self):
        self.id = 100

    async def fetch_message(self, message_id):
        return types.SimpleNamespace(embeds=[])


def _message(content):
    return types.SimpleNamespace(content=content, attachments=[], embeds=[],
                                 message_snapshots=[], id=200,
                                 channel=FakeChannel())


@pytest.fixture
def bot(monkeypatch):
    b = botmod.IndexBot.__new__(botmod.IndexBot)
    b._downloader = types.SimpleNamespace(session=lambda: 'session')
    b.sniffed = []

    async def fake_sniff(session, url, allowed_ext):
        b.sniffed.append(url)
        if url == BARE_FILE:
            return url, 'clip.mp4'
        return None

    monkeypatch.setattr(botmod.sniff, 'sniff', fake_sniff)
    monkeypatch.setattr(botmod.config, 'EMBED_UNFURL_WAIT', 0)
    monkeypatch.setattr(botmod.IndexBot, '_resolver_enabled',
                        staticmethod(lambda: False))
    return b


def test_a_link_serving_media_is_ingested_like_a_direct_file(bot):
    sources, _, urls = run(bot._gather_sources(_message(f'look {BARE_FILE}')))
    assert [(s.url, s.filename) for s in sources] == [(BARE_FILE, 'clip.mp4')]
    assert urls == [BARE_FILE]


def test_links_with_a_file_extension_are_not_asked(bot):
    sources, _, _ = run(bot._gather_sources(
        _message('https://cdn.example/a.mp4')))
    assert [s.filename for s in sources] == ['a.mp4']
    assert bot.sniffed == []


def test_links_something_else_claims_are_not_asked(bot, monkeypatch):
    """YouTube and TikTok are yt-dlp extractors'. Asking them what they
    serve would only cost a request each."""
    monkeypatch.setattr(botmod.IndexBot, '_resolver_enabled',
                        staticmethod(lambda: True))
    monkeypatch.setattr(botmod.resolver, 'filter_resolvable',
                        lambda urls: [u for u in urls
                                      if 'tiktok' in u or 'youtube' in u])
    run(bot._gather_sources(_message(
        'https://www.youtube.com/watch?v=x https://www.tiktok.com/@a/video/1 '
        'https://news.example/article')))
    assert bot.sniffed == ['https://news.example/article']


def test_a_link_dump_is_asked_about_only_a_few_links(bot):
    links = ' '.join(f'https://site{n}.example/x' for n in range(10))
    run(bot._gather_sources(_message(links)))
    assert len(bot.sniffed) == sniff.MAX_PER_MESSAGE
