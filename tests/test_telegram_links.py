"""telegram_bot: sending the bot a link.

The bot's other ingest route is a file Telegram already holds. A link is
different in the ways that matter to the two rules the bot is built on:

**The gate still comes first.** A link is a fetch the *sender* chose, made
from inside the stack's networks, so nothing may be resolved or downloaded
before the sender is known to be a linked index user. That is what the
gate tests here are about — not politeness, but security.

**Nothing untagged is indexed**, links included. An untagged link is
parked as a URL and asked about, exactly as an untagged photo is parked as
a file_id, so nothing is downloaded for a link nobody ever tags.

yt-dlp is not installed alongside the app's dependencies and is never run
here: `resolver` is faked so the tests describe the bot's decisions, not
the internet's. The doubles for Telegram and Redis come from
test_telegram_ingest, which owns them.
"""
import os
import types

import pytest

from bots_common import resolver, site_logins, sniff
from bots_common.index_client import IndexUnavailable
from telegram_bot import config, download
from telegram_bot.bot import IndexBot
from telegram_bot.pending import PendingStore
from test_telegram_ingest import (
    FakeContext,
    FakeIndex,
    FakeMessage,
    FakeRedis,
    FakeUpdate,
    FakeUser,
    run,
)

VIDEO_PAGE = 'https://www.youtube.com/watch?v=abc123&t=90'
DIRECT_GIF = 'https://example.com/media/cat.gif'
ARTICLE = 'https://example.com/2026/an-article'
# Names no file, but serves an mp4 — what the HEAD sniff is for.
BARE_FILE = 'https://files.example/ab/cd/abcdef0123456789abcdef0123456789'


# ── Doubles ───────────────────────────────────────────────────────────────
class FakeDownloader:
    """Stands in for LinkDownloader. 'Downloads' by writing a file where it
    is told, or raises whatever it was handed instead. Its sniff answers
    from `served` and records every link it was asked about."""

    def __init__(self, raises=None, payload=b'not really media'):
        self.raises = raises
        self.payload = payload
        self.calls = []
        self.sniffed = []
        self.served = {BARE_FILE: (BARE_FILE, '1234567 - tags.mp4')}

    async def sniff_links(self, urls, allowed_ext):
        self.sniffed.extend(urls)
        return {url: self.served[url] for url in urls if url in self.served}

    async def download(self, url, dst, label, max_bytes):
        self.calls.append({'url': url, 'dst': dst, 'max_bytes': max_bytes})
        if self.raises is not None:
            raise self.raises
        with open(dst, 'wb') as fh:
            fh.write(self.payload)

    async def close(self):
        pass


@pytest.fixture()
def store():
    return PendingStore(client=FakeRedis(), ttl=60, limit=5)


@pytest.fixture()
def downloader():
    return FakeDownloader()


@pytest.fixture()
def bot(store, downloader):
    return IndexBot(index=FakeIndex(), store=store, downloader=downloader)


@pytest.fixture()
def ctx(tmp_path):
    return FakeContext(tmp_path)


@pytest.fixture()
def ytdlp(monkeypatch, tmp_path):
    """A resolver that claims platform links and 'downloads' one by writing
    a file. `.error` makes the next resolve fail instead; `.name` is what
    yt-dlp is pretending to have produced."""
    state = types.SimpleNamespace(calls=[], error=None, name='A Video.mp4',
                                  site='youtube', path=None, rotate_to=None,
                                  cookies_seen=None)

    def filter_resolvable(urls):
        return [u for u in urls
                if 'youtube.com' in u or 'tiktok.com' in u]

    def resolve(url, tmpdir, max_bytes, cookies=None, stall_timeout=None,
                **kw):
        state.calls.append({'url': url, 'max_bytes': max_bytes,
                            'stall_timeout': stall_timeout,
                            'cookies': cookies, **kw})
        if cookies:
            with open(cookies, encoding='utf-8') as fh:
                state.cookies_seen = fh.read()
            if state.rotate_to is not None:
                with open(cookies, 'w', encoding='utf-8') as fh:
                    fh.write(state.rotate_to)
        if state.error is not None:
            raise state.error
        state.path = os.path.join(tmpdir, 'ytdlp-fake.out')
        with open(state.path, 'wb') as fh:
            fh.write(b'not really a video')
        return resolver.Resolved(state.path, state.name, state.site,
                                 bool(cookies))

    monkeypatch.setattr(resolver, 'available', lambda: True)
    monkeypatch.setattr(resolver, 'filter_resolvable', filter_resolvable)
    monkeypatch.setattr(resolver, 'resolve', resolve)
    monkeypatch.setattr(config, 'LINKS_ENABLED', True)
    monkeypatch.setattr(config, 'YTDLP_ENABLED', True)
    return state


def link_message(text, user=None, **kw):
    return FakeMessage(user=user or FakeUser(), text=text, **kw)


def send(bot, message, ctx):
    return run(bot.on_text(FakeUpdate(message), ctx))


def replies(message):
    return '\n'.join(message.replies)


# ── The gate ──────────────────────────────────────────────────────────────
def test_an_unlinked_sender_link_is_never_fetched(bot, ctx, store, ytdlp):
    """The whole point of the gate: a stranger must not be able to make
    this container fetch a URL of their choosing."""
    bot.index.owner = None
    msg = link_message(f'cats,funny {VIDEO_PAGE} {BARE_FILE}')
    send(bot, msg, ctx)
    assert ytdlp.calls == [], "the gate must run before any resolve"
    assert bot.downloads.sniffed == [], "…and before any sniff"
    assert bot.downloads.calls == []
    assert bot.index.uploads == []
    assert run(store.count(msg.from_user.id)) == 0


def test_an_unreachable_index_fetches_nothing_either(bot, ctx, ytdlp):
    """The gate fails closed — an outage pauses ingest, it does not open
    the fetch path to an unverified sender."""
    bot.index.raises = IndexUnavailable('down')
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert ytdlp.calls == [] and bot.index.uploads == []
    assert "can't reach the index" in replies(msg)


# ── A tagged link ingests straight away ───────────────────────────────────
def test_a_tagged_platform_link_is_resolved_and_indexed(bot, ctx, ytdlp):
    msg = link_message(f'cats,funny {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert [c['url'] for c in ytdlp.calls] == [VIDEO_PAGE]
    assert len(bot.index.uploads) == 1
    upload = bot.index.uploads[0]
    assert upload['filename'] == 'A Video.mp4'
    assert upload['uploaded_for'] == 'alice'


def test_the_source_site_rides_along_as_a_tag(bot, ctx, ytdlp):
    """Same as the Discord side: what a video came from is worth knowing
    in the gallery, and only the resolver can say."""
    send(bot, link_message(f'cats,funny {VIDEO_PAGE}'), ctx)
    assert bot.index.uploads[0]['tags'] == 'cats,funny,youtube,telegram'


def test_the_tag_rules_are_the_same_as_for_a_caption(bot, ctx, ytdlp, store):
    """Prose alongside a link is prose, not tags — so the link is held and
    asked about rather than indexed under 'look at this lol'."""
    msg = link_message(f'look at this lol {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert bot.index.uploads == [] and ytdlp.calls == []
    assert run(store.count(msg.from_user.id)) == 1


def test_a_direct_media_link_is_downloaded_not_resolved(bot, ctx, ytdlp):
    """A URL that already names a file needs a fetch, not an extractor."""
    send(bot, link_message(f'cats {DIRECT_GIF}'), ctx)
    assert ytdlp.calls == [], "yt-dlp has no business here"
    assert [c['url'] for c in bot.downloads.calls] == [DIRECT_GIF]
    assert bot.downloads.calls[0]['max_bytes'] == config.LINK_MAX_BYTES
    assert bot.index.uploads[0]['filename'] == 'cat.gif'
    assert bot.index.uploads[0]['tags'] == 'cats,telegram'


def test_a_link_serving_media_without_naming_it_is_downloaded(bot, ctx,
                                                               ytdlp):
    """Some CDNs serve an mp4 from …/ab/cd/<md5>. No extension and no
    extractor, but the server's Content-Type says what it is — so it is a
    direct download, under the filename the server offered."""
    send(bot, link_message(f'cats {BARE_FILE}'), ctx)
    assert ytdlp.calls == []
    assert [c['url'] for c in bot.downloads.calls] == [BARE_FILE]
    assert bot.index.uploads[0]['filename'] == '1234567 - tags.mp4'
    assert bot.index.uploads[0]['tags'] == 'cats,telegram'


def test_only_links_nothing_else_claims_are_sniffed(bot, ctx, ytdlp):
    """A file link needs no question and a platform page belongs to
    yt-dlp; asking either would only cost a request."""
    send(bot, link_message(
        f'cats {DIRECT_GIF} {VIDEO_PAGE} {BARE_FILE} {ARTICLE}'), ctx)
    assert bot.downloads.sniffed == [BARE_FILE, ARTICLE]


def test_sniffing_works_without_the_resolver(bot, ctx, monkeypatch, ytdlp):
    """It is a file-type check, not yt-dlp: a deployment without the
    resolver still takes a link that serves a file."""
    monkeypatch.setattr(resolver, 'available', lambda: False)
    send(bot, link_message(f'cats {BARE_FILE}'), ctx)
    assert [c['url'] for c in bot.downloads.calls] == [BARE_FILE]


def test_the_private_flag_applies_to_links_too(bot, ctx, ytdlp, store):
    user = FakeUser()
    run(store.set_flag(user.id, 'private', True))
    send(bot, link_message(f'secret {VIDEO_PAGE}', user=user), ctx)
    assert bot.index.uploads[0]['private'] is True


# ── An untagged link is parked, not fetched ───────────────────────────────
def test_a_bare_link_is_parked_and_asked_about(bot, ctx, store, ytdlp):
    """Nothing untagged is indexed, and — the reason this is worth a test —
    nothing untagged is *downloaded* either. What is held is the URL."""
    msg = link_message(VIDEO_PAGE)
    send(bot, msg, ctx)
    assert ytdlp.calls == [] and bot.index.uploads == []
    entries = run(store.list(msg.from_user.id))
    assert len(entries) == 1 and entries[0]['url'] == VIDEO_PAGE
    assert '🏷️' in replies(msg)


def test_tagging_a_parked_link_downloads_it(bot, ctx, store, ytdlp):
    user = FakeUser()
    send(bot, link_message(VIDEO_PAGE, user=user), ctx)
    send(bot, link_message('holiday,2026', user=user), ctx)
    assert [c['url'] for c in ytdlp.calls] == [VIDEO_PAGE]
    assert bot.index.uploads[0]['tags'] == 'holiday,2026,youtube,telegram'
    assert run(store.count(user.id)) == 0


def test_two_links_in_one_message_are_parked_separately(bot, ctx, store,
                                                        ytdlp):
    """One message, one message id — so parking by message id alone would
    have the second link quietly overwrite the first."""
    msg = link_message(f'{VIDEO_PAGE} {DIRECT_GIF}')
    send(bot, msg, ctx)
    entries = run(store.list(msg.from_user.id))
    assert sorted(e['url'] for e in entries) == sorted([VIDEO_PAGE, DIRECT_GIF])


def test_a_reply_to_the_link_message_tags_just_it(bot, ctx, store, ytdlp):
    """A link is not keyed by the message it arrived on, so the reply has
    to be matched by the message the entry remembers instead."""
    user = FakeUser()
    first = link_message(VIDEO_PAGE, user=user)
    send(bot, first, ctx)
    send(bot, link_message(DIRECT_GIF, user=user), ctx)

    reply = link_message('just-this', user=user, reply_to=first)
    send(bot, reply, ctx)
    assert [c['url'] for c in ytdlp.calls] == [VIDEO_PAGE]
    assert bot.index.uploads[0]['tags'] == 'just-this,youtube,telegram'
    assert run(store.count(user.id)) == 1, "the other link still waits"


def test_a_bare_sniffed_link_is_parked_with_what_it_serves(bot, ctx, store,
                                                           ytdlp):
    """Parked as the URL to fetch and the name to file it under — and
    downloaded only once tagged, like any other link."""
    user = FakeUser()
    send(bot, link_message(BARE_FILE, user=user), ctx)
    assert bot.downloads.calls == []
    [entry] = run(store.list(user.id))
    assert entry['url'] == BARE_FILE
    send(bot, link_message('holiday', user=user), ctx)
    assert [c['url'] for c in bot.downloads.calls] == [BARE_FILE]
    assert bot.index.uploads[0]['filename'] == '1234567 - tags.mp4'


def test_a_parked_link_is_only_taken_once(bot, ctx, store, ytdlp):
    """A second tag list must find the link gone, not download it again."""
    user = FakeUser()
    send(bot, link_message(VIDEO_PAGE, user=user), ctx)
    send(bot, link_message('once', user=user), ctx)
    send(bot, link_message('twice', user=user), ctx)
    assert len(bot.index.uploads) == 1


# ── Refusals ──────────────────────────────────────────────────────────────
def test_a_page_no_extractor_claims_is_refused(bot, ctx, store, ytdlp):
    """The extractor list is the gate that keeps arbitrary URLs — internal
    hosts included — out of the resolver. An unclaimed page is only asked
    what it serves (one HEAD, public addresses only), and unless that is
    media it is refused here rather than fetched blind."""
    msg = link_message(ARTICLE)
    send(bot, msg, ctx)
    assert bot.downloads.sniffed == [ARTICLE]
    assert ytdlp.calls == [] and bot.downloads.calls == []
    assert run(store.count(msg.from_user.id)) == 0
    assert "can't download anything" in replies(msg)


def test_the_refusal_says_when_the_resolver_is_missing(bot, ctx, monkeypatch,
                                                       ytdlp):
    """yt-dlp absent is an operator problem, not a bad link — say which."""
    monkeypatch.setattr(resolver, 'available', lambda: False)
    msg = link_message(VIDEO_PAGE)
    send(bot, msg, ctx)
    assert 'resolver is unavailable' in replies(msg)


# ── Site logins ───────────────────────────────────────────────────────────
STORED = '.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\told\n'
ROTATED = '.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tnew\n'


def test_the_stored_login_is_what_the_resolver_runs_with(bot, ctx, ytdlp,
                                                         monkeypatch):
    monkeypatch.setattr(config, 'YTDLP_QUALITY', '720')
    monkeypatch.setattr(config, 'YTDLP_POT_PROVIDER', 'http://bgutil:4416')
    bot.index.login = site_logins.StoredLogin('youtube', 'YouTube', 4, STORED)
    ytdlp.rotate_to = ROTATED

    send(bot, link_message(f'cats {VIDEO_PAGE}'), ctx)

    assert bot.index.login_requests == [VIDEO_PAGE]
    assert ytdlp.cookies_seen == STORED
    call = ytdlp.calls[0]
    assert call['quality'] == '720'
    assert call['pot_provider'] == 'http://bgutil:4416'
    assert not os.path.exists(call['cookies'])
    # The site rotated the session during a download that worked.
    assert bot.index.stored_logins == [('youtube', 4, ROTATED)]
    assert len(bot.index.uploads) == 1


def test_a_link_is_not_resolved_while_its_login_cannot_be_fetched(bot, ctx,
                                                                  ytdlp):
    bot.index.login_raises = IndexUnavailable('down')
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert ytdlp.calls == [] and bot.index.uploads == []
    assert "can't reach the index" in replies(msg)


def test_a_login_failure_says_what_an_admin_can_do(bot, ctx, ytdlp):
    ytdlp.error = resolver.ResolveError('This video is private', kind='auth')
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert 'no login is stored for that site' in replies(msg)
    assert site_logins.ADMIN_PAGE in replies(msg)


def test_a_resolver_failure_is_reported_verbatim(bot, ctx, ytdlp):
    """The resolver's reasons are specific ('no downloadable video found',
    'blocked by the site'); a generic 'it failed' would throw that away."""
    ytdlp.error = resolver.ResolveError('no downloadable video found')
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert bot.index.uploads == []
    assert 'no downloadable video found' in replies(msg)


def test_an_oversized_direct_download_is_refused(bot, ctx, ytdlp):
    bot.downloads.raises = download.TooLarge(900_000_000)
    msg = link_message(f'cats {DIRECT_GIF}')
    send(bot, msg, ctx)
    assert bot.index.uploads == []
    assert 'over the' in replies(msg) and 'limit' in replies(msg)


def test_a_resolved_file_of_an_unsupported_type_is_dropped(bot, ctx, ytdlp):
    """yt-dlp muxes to mp4 when it merges, but a site serving one
    pre-muxed format decides for itself — and the check cannot happen
    before the download, because nothing knows the type until then."""
    ytdlp.name = 'A Podcast.mp3'
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert bot.index.uploads == []
    assert 'does not take' in replies(msg)
    assert not os.path.exists(ytdlp.path), "the temp file must not be left"


def test_links_can_be_turned_off(bot, ctx, store, monkeypatch, ytdlp):
    monkeypatch.setattr(config, 'LINKS_ENABLED', False)
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert ytdlp.calls == [] and bot.downloads.calls == []
    assert run(store.count(msg.from_user.id)) == 0


# ── Replies ───────────────────────────────────────────────────────────────
def test_the_downloading_notice_is_taken_back_down(bot, ctx, ytdlp):
    """It says all it has to say while the download runs; leaving it there
    would put a dead 'Downloading…' line above every result."""
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    notices = [m for m in msg.sent if '⬇️' in m.text]
    assert len(notices) == 1 and notices[0].deleted


def test_a_url_is_html_escaped_in_replies(bot, ctx, ytdlp):
    """Every reply goes out as HTML and a query string is full of '&'.
    Unescaped, Telegram rejects the whole message with "can't parse
    entities" and the sender hears nothing back at all."""
    msg = link_message(f'cats {VIDEO_PAGE}')
    send(bot, msg, ctx)
    assert '&t=90' not in replies(msg)
    assert '&amp;t=90' in replies(msg)


# ── The public-address guard ──────────────────────────────────────────────
# There is no extractor gate on the direct-download path — whatever URL
# arrives is requested as-is — so this is the only thing standing between a
# link ending in .jpg and the stack's internal services.
class _Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(b'bytes',),
                 length=None):
        self.status = status
        self.headers = headers or {}
        self.content = _Content(chunks)
        self.content_length = length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested = []

    def get(self, url, allow_redirects=None):
        self.requested.append(url)
        return self.responses.pop(0)


def _resolving(monkeypatch, mapping):
    """Point DNS at a fixed answer, so the guard is tested and the network
    is not."""
    def getaddrinfo(host, port, **kw):
        return [(0, 0, 0, '', (address, port)) for address in mapping[host]]
    monkeypatch.setattr(sniff.socket, 'getaddrinfo', getaddrinfo)


def _downloader(monkeypatch, mapping, responses):
    _resolving(monkeypatch, mapping)
    dl = download.LinkDownloader()
    dl._session = _FakeSession(responses)
    return dl


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.5', '192.168.1.9',
                                     '172.16.4.1', '169.254.169.254', '::1',
                                     'fd00::1'])
def test_a_link_resolving_to_a_private_address_is_refused(monkeypatch,
                                                          address):
    _resolving(monkeypatch, {'internal.example': [address]})
    with pytest.raises(download.Blocked):
        sniff.require_public('http://internal.example/x.jpg')


def test_a_public_address_is_allowed(monkeypatch):
    _resolving(monkeypatch, {'example.com': ['93.184.216.34']})
    sniff.require_public('https://example.com/x.jpg')     # no raise


def test_one_private_record_among_public_ones_still_refuses(monkeypatch):
    """Checking only the first answer would make this a coin flip."""
    _resolving(monkeypatch, {'mixed.example': ['93.184.216.34', '127.0.0.1']})
    with pytest.raises(download.Blocked):
        sniff.require_public('http://mixed.example/x.jpg')


def test_a_non_http_scheme_is_refused():
    with pytest.raises(download.Blocked):
        sniff.require_public('file:///etc/passwd')


def test_a_redirect_into_an_internal_host_is_refused(monkeypatch, tmp_path):
    """The check is worth nothing if it only looks at what the user typed:
    hop one is public, hop two is not."""
    dl = _downloader(
        monkeypatch,
        {'public.example': ['93.184.216.34'],
         'internal.example': ['10.0.0.5']},
        [_FakeResponse(302, {'Location': 'http://internal.example/x.jpg'})])
    with pytest.raises(download.Blocked):
        run(dl.download('http://public.example/x.jpg', tmp_path / 'out',
                        'x.jpg', 1_000_000))
    assert dl._session.responses == [], "it must not fetch the second hop"


def test_a_redirect_between_public_hosts_is_followed(monkeypatch, tmp_path):
    dst = tmp_path / 'out'
    dl = _downloader(
        monkeypatch,
        {'a.example': ['93.184.216.34'], 'b.example': ['93.184.216.35']},
        [_FakeResponse(302, {'Location': 'https://b.example/cat.gif'}),
         _FakeResponse(chunks=(b'real ', b'bytes'))])
    run(dl.download('https://a.example/cat.gif', dst, 'cat.gif', 1_000_000))
    assert dst.read_bytes() == b'real bytes'
    assert dl._session.requested == ['https://a.example/cat.gif',
                                     'https://b.example/cat.gif']


def test_a_redirect_loop_gives_up(monkeypatch, tmp_path):
    dl = _downloader(
        monkeypatch, {'a.example': ['93.184.216.34']},
        [_FakeResponse(302, {'Location': 'https://a.example/cat.gif'})
         for _ in range(sniff.MAX_REDIRECTS + 1)])
    with pytest.raises(download.Blocked):
        run(dl.download('https://a.example/cat.gif', tmp_path / 'out',
                        'cat.gif', 1_000_000))


def test_a_declared_oversize_is_refused_before_a_byte_is_written(monkeypatch,
                                                                 tmp_path):
    dst = tmp_path / 'out'
    dl = _downloader(monkeypatch, {'a.example': ['93.184.216.34']},
                     [_FakeResponse(length=9_000_000, chunks=(b'x' * 10,))])
    with pytest.raises(download.TooLarge):
        run(dl.download('https://a.example/big.mp4', dst, 'big.mp4', 1_000))
    assert not dst.exists()


def test_an_undeclared_oversize_is_caught_mid_transfer(monkeypatch, tmp_path):
    """A chunked response carries no length to trust, so the running total
    is the only thing between a lying server and a full disk."""
    dl = _downloader(monkeypatch, {'a.example': ['93.184.216.34']},
                     [_FakeResponse(chunks=[b'x' * 400] * 10)])
    with pytest.raises(download.TooLarge):
        run(dl.download('https://a.example/big.mp4', tmp_path / 'out',
                        'big.mp4', 1_000))


def test_a_refused_link_is_explained_to_the_sender(bot, ctx, ytdlp):
    bot.downloads.raises = download.Blocked(
        "internal.example is not a public address, so I won't fetch it")
    msg = link_message(f'cats {DIRECT_GIF}')
    send(bot, msg, ctx)
    assert bot.index.uploads == []
    assert 'not a public address' in replies(msg)
