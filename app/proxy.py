"""Conditional ProxyFix — honour `X-Forwarded-*` only from a trusted peer.

Plain `ProxyFix` rewrites `REMOTE_ADDR` from `X-Forwarded-For` on *every*
request, no matter who connected. Behind the reverse proxy that is correct:
NPM sets the header with `$proxy_add_x_forwarded_for`, which appends the
real peer, and `x_for=1` reads the rightmost entry — so a client that sends
its own `X-Forwarded-For` cannot spoof anything.

It stops being correct the moment a request can arrive *without* going
through the proxy. Then the client supplies the whole header, the rightmost
entry is attacker-chosen, and `REMOTE_ADDR` becomes whatever they like —
which is the identity behind `is_ip_banned()`, `record_failed_login()` and
every `limiter.limit(...)` key. That buys an attacker unmetered login and
`/api/upload` brute force, plus the ability to ban a third party's address
by failing logins under it.

The published port is bound to loopback now, so that path is closed. This
middleware is the second line: it makes the guarantee a property of the
application rather than of one line in a compose file, so republishing the
port can never silently re-open it.

Requests from an untrusted peer keep their real `REMOTE_ADDR` and have the
forwarded headers stripped, so nothing further down the stack — including
WsgiDAV on the `/dav` branch, which never sees Flask's request object — can
be fooled by them either.
"""
import ipaddress
import logging

from werkzeug.middleware.proxy_fix import ProxyFix

#: Headers dropped for an untrusted peer. `ProxyFix` reads the first four;
#: `Forwarded` is stripped too so no future reader can be fooled by it.
_FORWARD_HEADERS = (
    'HTTP_X_FORWARDED_FOR',
    'HTTP_X_FORWARDED_PROTO',
    'HTTP_X_FORWARDED_HOST',
    'HTTP_X_FORWARDED_PREFIX',
    'HTTP_FORWARDED',
)

#: Trusted by default: loopback plus the private ranges. Docker bridge
#: networks live in 172.16.0.0/12, so the reverse proxy container is covered
#: without pinning a container IP that changes on every recreate. A direct
#: connection from a public address is *not* covered, which is the point.
DEFAULT_TRUSTED = (
    '127.0.0.0/8', '::1/128',
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    'fc00::/7',
)


def parse_networks(value):
    """Parse a comma-separated list of IPs/CIDRs into networks.

    Unparseable entries are logged and skipped rather than raising: a typo
    in this list must not take the whole app down at boot, and skipping an
    entry can only ever make the middleware *less* trusting.
    """
    nets = []
    for raw in (value or '').split(','):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logging.warning(
                "TRUSTED_PROXIES: ignoring unparseable entry %r", raw)
    return nets


class TrustedProxyFix:
    """Apply `ProxyFix` only when the connecting peer is trusted."""

    def __init__(self, app, trusted, **proxy_fix_kwargs):
        self.app = app
        self.trusted = list(trusted)
        self.proxied = ProxyFix(app, **proxy_fix_kwargs)

    def is_trusted(self, addr):
        if not addr:
            return False
        # A scoped IPv6 literal ('fe80::1%eth0') never parses; drop the zone.
        addr = addr.split('%', 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(ip in net for net in self.trusted)

    def __call__(self, environ, start_response):
        if self.is_trusted(environ.get('REMOTE_ADDR')):
            return self.proxied(environ, start_response)
        for header in _FORWARD_HEADERS:
            environ.pop(header, None)
        return self.app(environ, start_response)
