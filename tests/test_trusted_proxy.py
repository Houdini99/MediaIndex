"""app/proxy.py: X-Forwarded-* is honoured only from a trusted peer.

`REMOTE_ADDR` is the identity behind `is_ip_banned()`,
`record_failed_login()` and every rate-limit key, so whether a client can
choose it decides whether the ban system means anything.
These tests pin both halves: trusted peers still get the proxied address,
untrusted ones cannot influence it at all.
"""
from app.proxy import DEFAULT_TRUSTED, TrustedProxyFix, parse_networks


def make_app(trusted=DEFAULT_TRUSTED, **kw):
    """Wrap a WSGI app that reports what the stack decided REMOTE_ADDR is."""
    seen = {}

    def inner(environ, start_response):
        seen['remote_addr'] = environ.get('REMOTE_ADDR')
        seen['environ'] = environ
        start_response('200 OK', [])
        return [b'']

    kw.setdefault('x_for', 1)
    kw.setdefault('x_proto', 1)
    kw.setdefault('x_host', 1)
    kw.setdefault('x_prefix', 1)
    return TrustedProxyFix(inner, parse_networks(','.join(trusted)), **kw), seen


def call(app, peer, **headers):
    environ = {
        'REMOTE_ADDR': peer,
        'wsgi.url_scheme': 'http',
        'SERVER_NAME': 'test',
        'SERVER_PORT': '80',
        'REQUEST_METHOD': 'GET',
    }
    environ.update(headers)
    app(environ, lambda *a, **k: None)
    return environ


# ── The trusted path still works ───────────────────────────────────────────
def test_trusted_peer_gets_the_forwarded_address():
    """NPM sits on a docker bridge (172.16/12), so the real client address
    it appends must still win."""
    app, seen = make_app()
    call(app, '172.24.0.10', HTTP_X_FORWARDED_FOR='203.0.113.7')
    assert seen['remote_addr'] == '203.0.113.7'


def test_trusted_peer_takes_the_rightmost_entry():
    """nginx's $proxy_add_x_forwarded_for appends the real peer, so with
    x_for=1 a client-supplied prefix must be ignored."""
    app, seen = make_app()
    call(app, '172.24.0.10',
         HTTP_X_FORWARDED_FOR='1.2.3.4, 203.0.113.7')
    assert seen['remote_addr'] == '203.0.113.7'


def test_trusted_peer_still_gets_proto_and_host():
    app, seen = make_app()
    env = call(app, '172.24.0.10',
               HTTP_X_FORWARDED_FOR='203.0.113.7',
               HTTP_X_FORWARDED_PROTO='https',
               HTTP_X_FORWARDED_HOST='media.example.com')
    assert env['wsgi.url_scheme'] == 'https'
    assert env['HTTP_HOST'] == 'media.example.com'


# ── The untrusted path cannot influence anything ───────────────────────────
def test_untrusted_peer_keeps_its_real_address():
    """The whole point: a public peer reaching the app directly cannot
    become someone else."""
    app, seen = make_app()
    call(app, '198.51.100.9', HTTP_X_FORWARDED_FOR='203.0.113.7')
    assert seen['remote_addr'] == '198.51.100.9'


def test_untrusted_peer_has_forwarded_headers_stripped():
    """Stripped, not just ignored — the /dav branch never sees Flask's
    request object and must not find a usable header either."""
    app, _ = make_app()
    env = call(app, '198.51.100.9',
               HTTP_X_FORWARDED_FOR='203.0.113.7',
               HTTP_X_FORWARDED_PROTO='https',
               HTTP_X_FORWARDED_HOST='evil.example',
               HTTP_X_FORWARDED_PREFIX='/x',
               HTTP_FORWARDED='for=203.0.113.7')
    for header in ('HTTP_X_FORWARDED_FOR', 'HTTP_X_FORWARDED_PROTO',
                   'HTTP_X_FORWARDED_HOST', 'HTTP_X_FORWARDED_PREFIX',
                   'HTTP_FORWARDED'):
        assert header not in env


def test_untrusted_peer_cannot_forge_https():
    """X-Forwarded-Proto from an untrusted peer must not flip url_scheme —
    session cookies are Secure-only and key off it."""
    app, _ = make_app()
    env = call(app, '198.51.100.9', HTTP_X_FORWARDED_PROTO='https')
    assert env['wsgi.url_scheme'] == 'http'


def test_ban_poisoning_is_not_possible_from_an_untrusted_peer():
    """The concrete attack: fail logins under a victim's address so the
    victim is banned on the real site."""
    app, seen = make_app()
    call(app, '198.51.100.9', HTTP_X_FORWARDED_FOR='203.0.113.50')
    assert seen['remote_addr'] != '203.0.113.50'


# ── Trust list handling ────────────────────────────────────────────────────
def test_defaults_cover_docker_bridges_and_loopback():
    app, _ = make_app()
    for addr in ('127.0.0.1', '::1', '172.17.0.1', '172.24.0.10',
                 '10.1.2.3', '192.168.1.5'):
        assert app.is_trusted(addr), addr


def test_defaults_exclude_public_addresses():
    app, _ = make_app()
    for addr in ('198.51.100.9', '203.0.113.7', '8.8.8.8'):
        assert not app.is_trusted(addr), addr


def test_trust_list_can_be_narrowed_to_one_proxy():
    """Pinning the proxy's exact address is the stronger configuration."""
    app, seen = make_app(trusted=('172.24.0.10/32',))
    assert app.is_trusted('172.24.0.10')
    assert not app.is_trusted('172.24.0.11')
    # A neighbouring container on the same bridge is now untrusted.
    call(app, '172.24.0.11', HTTP_X_FORWARDED_FOR='203.0.113.7')
    assert seen['remote_addr'] == '172.24.0.11'


def test_missing_or_malformed_peer_is_untrusted():
    app, _ = make_app()
    for addr in (None, '', 'not-an-ip', 'unix'):
        assert not app.is_trusted(addr)


def test_scoped_ipv6_peer_is_handled():
    """A zone-suffixed literal must not raise on the way to a decision."""
    app, _ = make_app(trusted=('fe80::/10',))
    assert app.is_trusted('fe80::1%eth0')


def test_bad_trust_entries_are_skipped_not_fatal():
    """A typo in TRUSTED_PROXIES must not take the app down at boot; it can
    only ever make the middleware less trusting."""
    nets = parse_networks('172.24.0.0/16, nonsense, , 10.0.0.0/8')
    assert len(nets) == 2


def test_empty_trust_list_trusts_nobody():
    app, seen = make_app(trusted=())
    call(app, '172.24.0.10', HTTP_X_FORWARDED_FOR='203.0.113.7')
    assert seen['remote_addr'] == '172.24.0.10'
