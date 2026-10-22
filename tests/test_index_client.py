"""bots_common/index_client.py and the bots' subclasses: the /api/upload
transport, the identity lookups and their cache, and the cooldown that
keeps a burst of ingests from spending the index's IP-ban budget (10
failed logins in 15 minutes ban the container for 24 hours).

The API is the client's only transport, so the cooldown is tested there.

aiohttp is faked in conftest — the bot's dependencies are not installed
alongside the app's, and these tests are about the client's own logic, not
about HTTP.
"""
import asyncio

import pytest

from bots_common.index_client import AuthError, IndexUnavailable
from discord_bot.index_client import IndexClient
from telegram_bot.index_client import IndexClient as TelegramClient

def run(coro):
    return asyncio.run(coro)


# ── Fake HTTP ─────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status=200, text='', payload=None):
        self.status = status
        self._text = text
        self._payload = payload

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def get(self, url, **kw):
        return self._call('GET', url, kw)

    def post(self, url, **kw):
        return self._call('POST', url, kw)

    def put(self, url, **kw):
        return self._call('PUT', url, kw)

    def _call(self, method, url, kw):
        self.requests.append((method, url))
        return self.handler(method, url, kw)

    async def close(self):
        pass

    def count(self, method, suffix):
        return sum(1 for m, u in self.requests
                   if m == method and u.endswith(suffix))


def _client(handler, cls=IndexClient, **kw):
    client = cls('http://web:5001', 'Bot',
                 kw.pop('app_password', 'token'), **kw)
    client._session = FakeSession(handler)
    return client


# ── API transport ─────────────────────────────────────────────────────────
def _api_client(response, **kw):
    def handler(method, url, kw_):
        assert url.endswith('/api/upload'), url
        return response
    return _client(handler, **kw)


def test_api_upload_returns_hash_and_duplicate_details(tmp_path):
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    dup = {'id': 42, 'is_private': False, 'filepath': 'abc.png',
           'tags': 'cats', 'uploaded_by': 'bob'}
    client = _api_client(FakeResponse(payload={
        'results': [{'status': 'duplicate', 'filehash': 'abc',
                     'duplicate_of': dup}]}))
    result = run(client.upload(str(f), 'pic.png', 'cats'))
    assert result.status == 'duplicate'
    assert result.filehash == 'abc'
    assert result.duplicate == dup


def test_api_upload_sends_basic_auth_and_delegation(tmp_path):
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    seen = {}

    def handler(method, url, kw):
        seen.update(kw)
        return FakeResponse(payload={'results': [{'status': 'ok'}]})

    client = _client(handler, app_password='token')
    run(client.upload(str(f), 'pic.png', 'cats', uploaded_for='bob'))
    assert seen['auth'].login == 'Bot' and seen['auth'].password == 'token'
    fields = dict((n, v) for n, v, _ in seen['data'].fields)
    assert fields['tags'] == 'cats' and fields['uploaded_for'] == 'bob'
    assert 'private' not in fields     # public unless asked


def test_api_upload_sends_the_private_flag(tmp_path):
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    seen = {}

    def handler(method, url, kw):
        seen.update(kw)
        return FakeResponse(payload={'results': [{'status': 'ok'}]})

    client = _client(handler)
    run(client.upload(str(f), 'pic.png', 'cats', uploaded_for='bob',
                      private=True))
    fields = dict((n, v) for n, v, _ in seen['data'].fields)
    assert fields['private'] == '1' and fields['uploaded_for'] == 'bob'


def test_api_rejection_raises_and_starts_the_cooldown(tmp_path):
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    client = _api_client(FakeResponse(status=401))
    with pytest.raises(AuthError, match='app password'):
        run(client.upload(str(f), 'pic.png', 'cats'))
    with pytest.raises(AuthError, match='not retrying'):
        run(client.upload(str(f), 'pic.png', 'cats'))


def test_api_missing_endpoint_is_an_error_not_a_cooldown(tmp_path):
    """A 404 is a wrong INDEX_INTERNAL_URL, not a rejected credential: it
    can't feed the IP ban, so it fails the file like any other 4xx."""
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    client = _api_client(FakeResponse(status=404))
    result = run(client.upload(str(f), 'pic.png', 'cats'))
    assert result.status == 'error' and result.retryable is False
    assert result.detail == 'the index returned HTTP 404'
    assert client._blocked_until == 0


def test_api_server_error_is_retryable_but_a_client_error_is_not(tmp_path):
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    server = run(_api_client(FakeResponse(status=502))
                 .upload(str(f), 'pic.png', 'cats'))
    assert server.status == 'error' and server.retryable is True

    client_err = run(_api_client(FakeResponse(status=413))
                     .upload(str(f), 'pic.png', 'cats'))
    assert client_err.status == 'error' and client_err.retryable is False


def test_api_unreadable_reply_is_retryable(tmp_path):
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    client = _api_client(FakeResponse(status=200, text='<html>?</html>'))
    result = run(client.upload(str(f), 'pic.png', 'cats'))
    assert result.status == 'error' and result.retryable is True


def test_cooldown_expires(tmp_path):
    """Ported from the removed HTML tests: the block is a cooldown, not a
    permanent lockout — once it lapses the client tries the server again."""
    f = tmp_path / 'pic.png'
    f.write_bytes(b'x')
    rejecting = {'on': True}

    def handler(method, url, kw_):
        if rejecting['on']:
            return FakeResponse(status=401)
        return FakeResponse(status=200,
                            payload={'results': [{'status': 'ok'}]})

    client = _client(handler, auth_cooldown=900)
    with pytest.raises(AuthError, match='app password'):
        run(client.upload(str(f), 'pic.png', 'cats'))
    with pytest.raises(AuthError, match='not retrying'):
        run(client.upload(str(f), 'pic.png', 'cats'))

    client._blocked_until = 0        # pretend the cooldown elapsed
    rejecting['on'] = False
    assert run(client.upload(str(f), 'pic.png', 'cats')).status == 'ok'


# ── Discord identity lookup ───────────────────────────────────────────────
def _lookup_client(response):
    seen = {}

    def handler(method, url, kw):
        assert method == 'GET' and url.endswith('/api/discord/user'), url
        seen.update(kw)
        if isinstance(response, Exception):
            raise response
        return response

    return _client(handler), seen


def test_lookup_returns_the_linked_username():
    client, seen = _lookup_client(FakeResponse(payload={'username': 'alice'}))
    assert run(client.lookup_discord_user(111111111111111111)) == 'alice'
    # Sent as a string: a snowflake must not pass through a float anywhere.
    assert seen['params'] == {'discord_id': '111111111111111111'}
    assert seen['auth'].login == 'Bot'


def test_lookup_404_means_not_linked():
    client, _ = _lookup_client(FakeResponse(status=404))
    assert run(client.lookup_discord_user(1)) is None


def test_lookup_rejection_raises_and_starts_the_cooldown():
    client, _ = _lookup_client(FakeResponse(status=403))
    with pytest.raises(AuthError, match='DELEGATED_UPLOADERS'):
        run(client.lookup_discord_user(1))
    assert client._blocked_until > 0


@pytest.mark.parametrize('response', [
    FakeResponse(status=502),
    FakeResponse(status=200, text='<html>?</html>'),
    FakeResponse(status=200, payload={}),
])
def test_lookup_without_an_answer_is_unavailable_not_unlinked(response):
    """Only a 404 may read as "not linked" — anything else would tell a
    linked user to go and fix a link that is not broken."""
    client, _ = _lookup_client(response)
    with pytest.raises(IndexUnavailable):
        run(client.lookup_discord_user(1))


def test_lookup_network_error_is_unavailable():
    import aiohttp
    client, _ = _lookup_client(aiohttp.ClientError("connection refused"))
    with pytest.raises(IndexUnavailable, match='unreachable'):
        run(client.lookup_discord_user(1))


# ── Telegram identity lookup ──────────────────────────────────────────────
def _telegram_client(response):
    seen = []

    def handler(method, url, kw):
        assert method == 'GET' and url.endswith('/api/telegram/user'), url
        seen.append(kw)
        return response

    return _client(handler, cls=TelegramClient), seen


def test_telegram_lookup_sends_the_id_and_the_username():
    client, seen = _telegram_client(FakeResponse(payload={'username': 'alice'}))
    assert run(client.lookup_telegram_user(5, 'alice_tg')) == 'alice'
    assert seen[0]['params'] == {'telegram_id': '5', 'username': 'alice_tg'}
    assert run(client.lookup_telegram_user(6)) == 'alice'
    assert seen[1]['params'] == {'telegram_id': '6'}


def test_telegram_lookup_404_means_not_linked():
    client, _ = _telegram_client(FakeResponse(status=404))
    assert run(client.lookup_telegram_user(5)) is None


def test_telegram_lookup_rejection_raises_and_starts_the_cooldown():
    client, _ = _telegram_client(FakeResponse(status=401))
    with pytest.raises(AuthError, match='DELEGATED_UPLOADERS'):
        run(client.lookup_telegram_user(5))
    with pytest.raises(AuthError, match='not retrying'):
        run(client.lookup_telegram_user(5))


@pytest.mark.parametrize('response', [
    FakeResponse(status=502),
    FakeResponse(status=200, text='<html>?</html>'),
    FakeResponse(status=200, payload={}),
])
def test_telegram_lookup_without_an_answer_is_unavailable(response):
    """An unreadable reply used to escape the Telegram client as a bare
    ValueError; it is an outage like any other now."""
    client, _ = _telegram_client(response)
    with pytest.raises(IndexUnavailable):
        run(client.lookup_telegram_user(5))


# ── The lookup cache ──────────────────────────────────────────────────────
class _Links:
    """An identity endpoint that answers from `links` (missing = 404) and
    counts the requests, or raises `outage` instead when it is set."""

    def __init__(self, links):
        self.links = links
        self.outage = None
        self.asked = []

    def __call__(self, method, url, kw):
        fields = kw.get('params') or kw['data']
        uid = int(fields.get('discord_id') or fields['telegram_id'])
        if url.endswith('/api/discord/verify'):
            self.links[uid] = 'alice'
            return FakeResponse(payload={'username': 'alice'})
        self.asked.append(uid)
        if self.outage:
            return FakeResponse(status=self.outage)
        if uid in self.links:
            return FakeResponse(payload={'username': self.links[uid]})
        return FakeResponse(status=404)


@pytest.mark.parametrize('cls, lookup', [
    (IndexClient, 'lookup_discord_user'),
    (TelegramClient, 'lookup_telegram_user'),
])
def test_answers_are_cached_not_linked_included(cls, lookup):
    links = _Links({42: 'alice'})
    client = _client(links, cls=cls)
    ask = getattr(client, lookup)
    assert run(ask(42)) == 'alice' and run(ask(42)) == 'alice'
    assert run(ask(7)) is None and run(ask(7)) is None
    assert links.asked == [42, 7]


@pytest.mark.parametrize('cls, lookup', [
    (IndexClient, 'lookup_discord_user'),
    (TelegramClient, 'lookup_telegram_user'),
])
def test_an_outage_is_not_cached(cls, lookup):
    """Caching it would keep a linked user unlinked for the rest of the
    TTL after the index came back."""
    links = _Links({42: 'alice'})
    links.outage = 502
    client = _client(links, cls=cls)
    ask = getattr(client, lookup)
    with pytest.raises(IndexUnavailable):
        run(ask(42))
    links.outage = None
    assert run(ask(42)) == 'alice'
    assert links.asked == [42, 42]


def test_cached_answers_expire():
    links = _Links({})
    client = _client(links, lookup_ttl=60)
    assert run(client.lookup_discord_user(42)) is None
    links.links[42] = 'alice'
    assert run(client.lookup_discord_user(42)) is None      # still cached
    client._owners[42] = (None, 0)                           # TTL elapsed
    assert run(client.lookup_discord_user(42)) == 'alice'


def test_a_redeemed_link_code_drops_the_cached_answer():
    links = _Links({})
    client = _client(links)
    assert run(client.lookup_discord_user(42)) is None      # caches "not linked"
    assert run(client.verify_discord_link(42, 'K7QX-M2PA')) == 'alice'
    # The cached "not linked" must not outlive the link.
    assert run(client.lookup_discord_user(42)) == 'alice'
    assert links.asked == [42, 42]


# ── Redeeming a link code ─────────────────────────────────────────────────
def _verify_client(response):
    seen = {}

    def handler(method, url, kw):
        assert method == 'POST' and url.endswith('/api/discord/verify'), url
        seen.update(kw)
        return response

    return _client(handler), seen


def test_verify_returns_the_linked_username():
    client, seen = _verify_client(FakeResponse(payload={'username': 'alice'}))
    assert run(client.verify_discord_link(111111111111111111,
                                          'K7QX-M2PA')) == 'alice'
    assert seen['data'] == {'discord_id': '111111111111111111',
                            'code': 'K7QX-M2PA'}


@pytest.mark.parametrize('status', [404, 400])
def test_verify_wrong_or_malformed_code_is_none(status):
    client, _ = _verify_client(FakeResponse(status=status))
    assert run(client.verify_discord_link(1, 'AAAA-AAAA')) is None


def test_verify_rejection_raises_and_starts_the_cooldown():
    client, _ = _verify_client(FakeResponse(status=401))
    with pytest.raises(AuthError):
        run(client.verify_discord_link(1, 'AAAA-AAAA'))
    assert client._blocked_until > 0


def test_verify_server_error_is_unavailable():
    client, _ = _verify_client(FakeResponse(status=503))
    with pytest.raises(IndexUnavailable):
        run(client.verify_discord_link(1, 'AAAA-AAAA'))


# ── Site logins ───────────────────────────────────────────────────────────
LOGIN_REPLY = {'platform': 'youtube', 'name': 'YouTube', 'revision': 7,
               'cookies': '# Netscape HTTP Cookie File\n'}


def _site_client(response, seen=None):
    def handler(method, url, kw):
        if seen is not None:
            seen.append((method, url, kw))
        return response
    return _client(handler)


def test_site_login_returns_the_stored_login():
    seen = []
    client = _site_client(FakeResponse(payload=LOGIN_REPLY), seen)
    login = run(client.site_login('https://youtu.be/1'))
    assert (login.platform, login.name, login.revision) == \
        ('youtube', 'YouTube', 7)
    method, url, kw = seen[0]
    assert method == 'GET' and url.endswith('/api/site-logins/cookies')
    assert kw['params'] == {'url': 'https://youtu.be/1'}


def test_no_stored_login_is_none_not_an_error():
    client = _site_client(FakeResponse(status=404, text='not found'))
    assert run(client.site_login('https://youtu.be/1')) is None


def test_a_rejected_app_password_starts_the_cooldown_for_logins_too():
    client = _site_client(FakeResponse(status=403))
    with pytest.raises(AuthError):
        run(client.site_login('https://youtu.be/1'))
    with pytest.raises(AuthError, match='not retrying'):
        run(client.site_login('https://youtu.be/1'))


@pytest.mark.parametrize('response', [
    FakeResponse(status=502),
    FakeResponse(payload={'platform': 'youtube'}),
    FakeResponse(text='<html>'),
])
def test_no_usable_answer_is_index_unavailable(response):
    """Never "no login": resolving logged out on an outage would settle a
    link as "login required"."""
    with pytest.raises(IndexUnavailable):
        run(_site_client(response).site_login('https://youtu.be/1'))


def test_store_site_login_names_the_revision_it_started_from():
    from bots_common.site_logins import StoredLogin
    seen = []
    client = _site_client(FakeResponse(payload={'revision': 8}), seen)
    login = StoredLogin('youtube', 'YouTube', 7, 'old')
    assert run(client.store_site_login(login, 'new')) == 8
    method, url, kw = seen[0]
    assert method == 'PUT'
    assert url.endswith('/api/site-logins/youtube/cookies')
    assert kw['json'] == {'revision': 7, 'cookies': 'new'}


@pytest.mark.parametrize('status', [409, 422, 400])
def test_a_refused_write_back_is_none(status):
    from bots_common.site_logins import StoredLogin
    client = _site_client(FakeResponse(status=status, payload={}))
    login = StoredLogin('youtube', 'YouTube', 7, 'old')
    assert run(client.store_site_login(login, 'new')) is None
