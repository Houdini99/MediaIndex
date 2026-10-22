"""Reading the bots' settings from the environment.

Both bots read their configuration once, at import time, and fail fast on
anything required: a typo in a variable should name itself at startup,
not surface three hours later as a bot that silently ingests nothing.
These are the readers both config modules use, plus the yt-dlp block the
two bots share apart from its DISCORD_/TELEGRAM_ prefix.
"""
import os
from dataclasses import dataclass

YTDLP_QUALITIES = ('best', '2160', '1440', '1080', '720', '480')


def require(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value


def num(name, default=None, cast=float):
    """Numeric env var. A typo names itself instead of surfacing as a bare
    ValueError traceback; with no default the variable is required."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        if default is None:
            raise RuntimeError(f"{name} environment variable is not set")
        return cast(default)
    try:
        return cast(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None


def flag(name, default):
    """On/off env var. Only the word for the other state flips it: 'false'
    turns off a switch that defaults on, 'true' turns on one that defaults
    off. Anything else, empty included, keeps the default."""
    raw = os.environ.get(name, '').strip().lower()
    return raw != 'false' if default else raw == 'true'


@dataclass(frozen=True)
class YtdlpSettings:
    """The yt-dlp resolver's settings, the same for both bots."""
    # {prefix}_YTDLP: 'false' disables the resolver (a missing yt-dlp
    # module disables it too).
    enabled: bool
    # {prefix}_YTDLP_MAX_MB, in bytes.
    max_bytes: int
    # {prefix}_YTDLP_STALL_TIMEOUT: a download is aborted only when it
    # *stalls*, no new bytes for this many seconds. It watches progress,
    # not total runtime, so a large-but-healthy download is never cut off
    # (a 1 GB file at 3 MB/s is fine; a frozen one dies). The abort
    # happens inside yt-dlp, so the transfer actually stops.
    stall_timeout: float
    # {prefix}_YTDLP_MAX_DURATION: absolute backstop for a resolve that
    # hangs *before* the download starts (extraction, where the stall
    # watchdog can't fire yet). Generous on purpose: a safety net, not
    # the primary limit.
    max_duration: float
    # {prefix}_YTDLP_COOKIES: logins for sites that hide videos behind one
    # (YouTube, Instagram, X, Facebook, Reddit, …) live in the index: an
    # admin uploads a cookies.txt per site on /admin/site-logins, and the
    # bot fetches the one a link needs before resolving it
    # (bots_common/site_logins.py). This optional mounted Netscape
    # cookies.txt is only the fallback for a link whose site has no login
    # stored there. Empty = none.
    cookies: str
    # {prefix}_YTDLP_QUALITY: resolution cap, one of YTDLP_QUALITIES. A
    # preference, not a filter (see resolver._format_sort), with H.264
    # preferred at any height. 1080, not 'best': the bots index clips, not
    # archival masters, and a 4K pull is mostly VP9/AV1 that blows the
    # 512 MB cap anyway.
    quality: str
    # {prefix}_YTDLP_POT_PROVIDER: the bgutil PO-token server (the
    # `bgutil` sidecar in the stack). It answers YouTube's "Sign in to
    # confirm you're not a bot" for a server IP, so most public videos
    # need no login at all. Empty disables it.
    pot_provider: str


def ytdlp_settings(prefix):
    """The {prefix}_YTDLP* variables, e.g. DISCORD_YTDLP_QUALITY for
    prefix 'DISCORD'."""
    var = f'{prefix}_YTDLP'
    quality = os.environ.get(f'{var}_QUALITY', '1080').strip()
    if quality not in YTDLP_QUALITIES:
        raise RuntimeError(
            f"{var}_QUALITY must be one of {'/'.join(YTDLP_QUALITIES)}, "
            f"got {quality!r}")
    return YtdlpSettings(
        enabled=flag(var, True),
        max_bytes=int(num(f'{var}_MAX_MB', 512) * 1e6),
        stall_timeout=num(f'{var}_STALL_TIMEOUT', 120),
        max_duration=num(f'{var}_MAX_DURATION', 1800),
        cookies=os.environ.get(f'{var}_COOKIES', '').strip(),
        quality=quality,
        pot_provider=os.environ.get(f'{var}_POT_PROVIDER', '').strip().rstrip('/'),
    )
