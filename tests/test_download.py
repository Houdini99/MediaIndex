"""discord_bot/download.py — streaming to disk, the address check and
link cap, and Discord CDN re-signing.

This code had no coverage before it was split out of bot.py. The
extraction itself was a pure move, but these behaviours are subtle
enough to be worth pinning permanently:

  * a 403/404 is only retried when the host is a Discord CDN — retrying a
    genuine 403 from anywhere else would hammer a server that has already
    said no;
  * the *original* error is what surfaces when re-signing fails, so the log
    shows why the download actually died rather than a secondary failure;
  * a link in a message is fetched only from a public address, every
    redirect hop included, and only up to DISCORD_LINK_MAX_MB.

Follows the fake-session pattern from test_index_client.py: the session is
injected rather than built, and DNS is faked, so nothing here opens a
socket.
"""
import asyncio

import aiohttp
import pytest

from bots_common import sniff
from bots_common.download import TooLarge
from discord_bot import config, download


def run(coro):
    return asyncio.run(coro)


# ── Fake HTTP ─────────────────────────────────────────────────────────────
class FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, chunks=(), payload=None, headers=None,
                 length=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)
        self.content_length = length or sum(len(c) for c in chunks) or None
        self._payload = payload

    def raise_for_status(self):
        if self.status >= 400:
            err = aiohttp.ClientResponseError()
            err.status = self.status
            raise err

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Serves queued responses per verb and records every request."""

    def __init__(self, get=(), post=()):
        self._get = list(get)
        self._post = list(post)
        self.gets = []
        self.get_kwargs = []
        self.posts = []
        self.closed = False

    def get(self, url, **kw):
        self.gets.append(url)
        self.get_kwargs.append(kw)
        return self._get.pop(0)

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self._post.pop(0)

    async def close(self):
        self.closed = True


class Src:
    def __init__(self, url, filename='clip.mp4'):
        self.url = url
        self.filename = filename


CDN = 'https://cdn.discordapp.com/attachments/1/2/clip.mp4'
FRESH = 'https://cdn.discordapp.com/attachments/1/2/clip.mp4?ex=refreshed'
ELSEWHERE = 'https://example.com/clip.mp4'
INTERNAL = 'http://web:5001/media/clip.mp4'

#: What the fake DNS answers. `web` is the index container's name on the
#: stack's networks — the obvious thing a hostile link would point at.
ADDRESSES = {'cdn.discordapp.com': ['162.159.130.233'],
             'example.com': ['93.184.216.34'],
             'web': ['172.18.0.5']}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def getaddrinfo(host, port, **kw):
        return [(0, 0, 0, '', (address, port)) for address in ADDRESSES[host]]
    monkeypatch.setattr(sniff.socket, 'getaddrinfo', getaddrinfo)


def _dl(session):
    d = download.CdnDownloader()
    d._session = session          # injected; session() never builds a real one
    return d


def _refresh_ok(url=FRESH):
    return FakeResponse(payload={'refreshed_urls': [
        {'refreshed': url}]})


# ── stream ────────────────────────────────────────────────────────────────
def test_stream_writes_every_chunk_to_disk(tmp_path):
    dst = tmp_path / 'out.bin'
    session = FakeSession(get=[FakeResponse(chunks=[b'abc', b'def', b'gh'])])

    run(_dl(session).stream(CDN, str(dst), 'clip.mp4'))

    assert dst.read_bytes() == b'abcdefgh'


def test_stream_truncates_a_partial_previous_attempt(tmp_path):
    """dst is reopened per call, so a retry must not append to the leftovers
    of the first try."""
    dst = tmp_path / 'out.bin'
    dst.write_bytes(b'XXXXXXXXXXXXXXXX')
    session = FakeSession(get=[FakeResponse(chunks=[b'new'])])

    run(_dl(session).stream(CDN, str(dst), 'clip.mp4'))

    assert dst.read_bytes() == b'new'


def test_stream_raises_on_http_error(tmp_path):
    session = FakeSession(get=[FakeResponse(status=500)])

    with pytest.raises(aiohttp.ClientResponseError):
        run(_dl(session).stream(CDN, str(tmp_path / 'x'), 'clip.mp4'))


# ── stream: public addresses only, links capped ───────────────────────────
def test_a_link_to_an_internal_host_is_never_requested(tmp_path):
    session = FakeSession(get=[FakeResponse(chunks=[b'secret'])])

    with pytest.raises(sniff.Blocked):
        run(_dl(session).stream(INTERNAL, str(tmp_path / 'x'), 'clip.mp4'))

    assert session.gets == []


def test_a_redirect_into_an_internal_host_is_refused(tmp_path):
    """Hop one is public, hop two is not: the check must see every hop."""
    session = FakeSession(get=[FakeResponse(302, headers={'Location': INTERNAL}),
                               FakeResponse(chunks=[b'secret'])])

    with pytest.raises(sniff.Blocked):
        run(_dl(session).stream(ELSEWHERE, str(tmp_path / 'x'), 'clip.mp4'))

    assert session.gets == [ELSEWHERE]
    assert session.get_kwargs[0]['allow_redirects'] is False


def test_a_redirect_between_public_hosts_is_followed(tmp_path):
    dst = tmp_path / 'out.bin'
    session = FakeSession(get=[FakeResponse(301, headers={'Location': CDN}),
                               FakeResponse(chunks=[b'ok'])])

    run(_dl(session).stream(ELSEWHERE, str(dst), 'clip.mp4'))

    assert dst.read_bytes() == b'ok'
    assert session.gets == [ELSEWHERE, CDN]


def test_a_link_over_the_cap_is_refused_before_a_byte_is_written(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'LINK_MAX_BYTES', 1_000)
    dst = tmp_path / 'out.bin'
    session = FakeSession(get=[FakeResponse(chunks=[b'x'], length=9_000_000)])

    with pytest.raises(TooLarge) as exc:
        run(_dl(session).stream(ELSEWHERE, str(dst), 'clip.mp4'))

    assert exc.value.size == 9_000_000
    assert not dst.exists()


def test_a_link_without_a_length_is_stopped_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'LINK_MAX_BYTES', 1_000)
    session = FakeSession(get=[FakeResponse(chunks=[b'x' * 400] * 10)])
    session._get[0].content_length = None   # chunked: nothing to trust

    with pytest.raises(TooLarge):
        run(_dl(session).stream(ELSEWHERE, str(tmp_path / 'x'), 'clip.mp4'))


def test_an_attachment_on_the_cdn_is_not_capped(tmp_path, monkeypatch):
    """Discord's upload limit bounds attachments; the link cap must not
    refuse a big one that Discord itself accepted."""
    monkeypatch.setattr(config, 'LINK_MAX_BYTES', 1_000)
    dst = tmp_path / 'out.bin'
    session = FakeSession(get=[FakeResponse(chunks=[b'x' * 400] * 10)])

    run(_dl(session).stream(CDN, str(dst), 'clip.mp4'))

    assert dst.stat().st_size == 4_000


# ── download: the re-signing retry ────────────────────────────────────────
def test_download_does_not_refresh_when_the_first_try_works(tmp_path):
    dst = tmp_path / 'out.bin'
    session = FakeSession(get=[FakeResponse(chunks=[b'ok'])])

    run(_dl(session).download(Src(CDN), str(dst)))

    assert dst.read_bytes() == b'ok'
    assert session.posts == []            # never asked Discord to re-sign


@pytest.mark.parametrize('status', [403, 404])
def test_download_resigns_an_expired_cdn_url_and_retries_once(tmp_path, status):
    dst = tmp_path / 'out.bin'
    session = FakeSession(
        get=[FakeResponse(status=status), FakeResponse(chunks=[b'fresh'])],
        post=[_refresh_ok()])

    run(_dl(session).download(Src(CDN), str(dst)))

    assert dst.read_bytes() == b'fresh'
    assert session.gets == [CDN, FRESH]   # retried against the re-signed URL
    assert len(session.posts) == 1


def test_download_does_not_refresh_a_non_discord_host(tmp_path):
    """A 403 from anywhere else is a real refusal — retrying would just
    hammer a server that already said no."""
    session = FakeSession(get=[FakeResponse(status=403)])

    with pytest.raises(aiohttp.ClientResponseError):
        run(_dl(session).download(Src(ELSEWHERE), str(tmp_path / 'x')))

    assert session.posts == []


def test_download_reraises_the_original_error_when_refresh_fails(tmp_path):
    """The surfaced error must be the download's 403, not a secondary
    failure from the refresh call."""
    session = FakeSession(get=[FakeResponse(status=403)],
                          post=[FakeResponse(status=500)])

    with pytest.raises(aiohttp.ClientResponseError) as exc:
        run(_dl(session).download(Src(CDN), str(tmp_path / 'x')))

    assert exc.value.status == 403
    assert len(session.gets) == 1         # no second download attempt


def test_download_reraises_when_refresh_returns_no_url(tmp_path):
    session = FakeSession(get=[FakeResponse(status=403)],
                          post=[FakeResponse(payload={'refreshed_urls': []})])

    with pytest.raises(aiohttp.ClientResponseError):
        run(_dl(session).download(Src(CDN), str(tmp_path / 'x')))


def test_download_propagates_a_non_expiry_status_untouched(tmp_path):
    session = FakeSession(get=[FakeResponse(status=500)])

    with pytest.raises(aiohttp.ClientResponseError) as exc:
        run(_dl(session).download(Src(CDN), str(tmp_path / 'x')))

    assert exc.value.status == 500
    assert session.posts == []


# ── refresh_cdn_url ───────────────────────────────────────────────────────
def test_refresh_returns_the_new_url():
    session = FakeSession(post=[_refresh_ok()])

    assert run(_dl(session).refresh_cdn_url(CDN)) == FRESH
    url, kw = session.posts[0]
    assert url == download.REFRESH_URL_ENDPOINT
    assert kw['json'] == {'attachment_urls': [CDN]}
    assert kw['headers']['Authorization'].startswith('Bot ')


def test_refresh_returns_none_on_http_error():
    session = FakeSession(post=[FakeResponse(status=401)])
    assert run(_dl(session).refresh_cdn_url(CDN)) is None


def test_refresh_returns_none_when_discord_declines_to_refresh():
    session = FakeSession(post=[FakeResponse(
        payload={'refreshed_urls': [{'refreshed': None}]})])
    assert run(_dl(session).refresh_cdn_url(CDN)) is None


def test_refresh_never_raises_on_a_transport_failure():
    """Best-effort by contract: the caller falls back to the original error."""
    class Boom(FakeSession):
        def post(self, url, **kw):
            raise aiohttp.ClientError('connection reset')

    assert run(_dl(Boom()).refresh_cdn_url(CDN)) is None


# ── session lifecycle ─────────────────────────────────────────────────────
def test_close_closes_the_session_and_allows_a_fresh_one():
    session = FakeSession()
    d = _dl(session)

    run(d.close())

    assert session.closed
    assert d._session is None             # a later call builds a new one


def test_close_is_safe_when_no_session_was_ever_opened():
    run(download.CdnDownloader().close())   # must not raise
