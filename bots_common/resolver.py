"""yt-dlp resolution for platform links (YouTube, TikTok, Twitter, …).

The normal ingest paths handle uploaded files, direct media links, and
media behind embeds; a link that yields none of those is offered to
yt-dlp here. Everything is best-effort by design: yt-dlp not installed,
no extractor matching the URL, or a failed/oversized download all report
"can't" (False/None) instead of raising, so each bot's own 🚫/❌
vocabulary stays the fallback. Import stays guarded and config-free so
the module is unit-testable without yt-dlp (or a bot library) installed.

Shared by both bots (bots_common/): nothing here is specific to either,
and its only dependencies are the sibling parsing module's
`format_progress` and ytdlp_fixes' stopgaps for broken extractors.

Three things make a cookieless server's yt-dlp hold up against the big
platforms, and each is a parameter rather than a lookup here: a cookies
file with the site's login (site_logins.py fetches it from the index), a
PO-token provider for YouTube's "confirm you're not a bot" check, and a
quality cap with H.264 preferred, so a 4K VP9 master neither blows the
size cap nor lands as a file half the browsers can't play.
"""
import asyncio
import contextlib
import glob
import logging
import os
import re
import tempfile
import time
import urllib.parse
import uuid

from . import parsing, ytdlp_fixes

try:
    import yt_dlp
except Exception:            # not installed / broken install → resolver off
    yt_dlp = None
else:
    # Extractors a site redesign broke, patched until yt-dlp catches up.
    ytdlp_fixes.apply()

# Seconds between progress lines in the container log while downloading.
PROGRESS_INTERVAL = 5.0

# Downloads yt-dlp is still writing use these suffixes; never treat them
# as a finished file, never ingest them.
_PARTIAL_SUFFIXES = ('.part', '.ytdl', '.temp')


class ResolveError(Exception):
    """A per-link resolve failure carrying a human-readable reason (for the
    bot-logs channel), the reaction emoji it maps to, and whether trying
    again later could plausibly succeed. resolve() raises this — rather
    than returning None — for every expected failure, so the caller can
    report *why* instead of a generic "couldn't resolve".

    `retryable` drives the bot's retry queue: a stalled or failed transfer
    is worth another attempt later, while "no video on that page", "over
    the size cap" and "the site blocks us" are settled answers that would
    only waste bandwidth.

    `kind` is 'auth' when the site wanted a login (or a fresher one) — the
    one failure an admin can fix, by uploading cookies — 'formats' when it
    returned nothing downloadable, and None otherwise.
    """

    def __init__(self, reason, emoji='🚫', retryable=False, kind=None):
        super().__init__(reason)
        self.reason = reason
        self.emoji = emoji
        self.retryable = retryable
        self.kind = kind


class DownloadStalled(ResolveError):
    """A download aborted from inside the progress hook because it stopped
    making progress within the stall window."""

    def __init__(self, reason):
        super().__init__(reason, emoji='❌', retryable=True)


def _short_error(exc):
    """A one-line summary of an exception for a status post — yt-dlp errors
    can be long and multi-line."""
    text = ' '.join(str(exc).split()) or type(exc).__name__
    return text[:197] + '…' if len(text) > 198 else text


#: yt-dlp formats its errors for a console: 'ERROR: [twitter] <id>: the
#: real message'. None of that prefix earns its place in a chat reply that
#: already names the link.
_ERROR_PREFIX_RE = re.compile(r'^ERROR:\s*(?:\[[^\]]+\]\s*)?(?:[^\s:]+:\s+)?')


def _clean_error(exc):
    """`_short_error` with yt-dlp's console prefix taken off."""
    return _ERROR_PREFIX_RE.sub('', _short_error(exc), count=1)


def _expected(exc):
    """True when yt-dlp itself classed this failure as an *answer* rather
    than a fault — 'No video could be found in this tweet', 'Video
    unavailable', 'Private video', a geo block.

    The flag lives on the ExtractorError, but by the time the failure
    reaches us YoutubeDL has caught that and re-raised a DownloadError,
    which carries the original in `exc_info` — so both are worth looking
    at, and anything without the attribute (a plain RuntimeError, a
    network error) is treated as unexpected.
    """
    if getattr(exc, 'expected', False):
        return True
    info = getattr(exc, 'exc_info', None) or ()
    return any(getattr(item, 'expected', False) for item in info)


#: yt-dlp's ways of saying "log in first". Lower-cased substrings of its
#: message; YouTube's bot check is among them, because a login (or a
#: fresher one) is what clears it when the PO token alone does not.
_AUTH_PATTERNS = (
    'sign in to confirm',
    "confirm you're not a bot",
    'confirm your age',
    'use --cookies',
    'cookies-from-browser',
    'login required',
    'login_required',
    'requires authentication',
    'authentication required',
    'you must be logged in',
    'logged-in',
    'private video',
    'this video is private',
    'members-only',
    'members only',
    'age-restricted',
    'age restricted',
    'nsfw tweet',
    'empty media response',
    'rate-limit reached or login',
)

#: …and of saying "nothing downloadable came back", which on YouTube is
#: mostly a stale login starving the format list.
_FORMAT_PATTERNS = (
    'requested format is not available',
    'only images are available',
    'no video formats found',
)


def _classify(exc):
    """'auth', 'formats' or None for a yt-dlp failure."""
    text = str(exc).lower()
    if any(p in text for p in _AUTH_PATTERNS):
        return 'auth'
    if any(p in text for p in _FORMAT_PATTERNS):
        return 'formats'
    return None


def _download_failure(url, exc, action="download it"):
    """The ResolveError for a failed yt-dlp run, plus its log line.

    Splitting these two verdicts apart is the whole point. A tweet with no
    video in it is not a broken transfer; treating it as one would mean a
    full stack trace in the container log, a ❌, and a queued retry that
    could only ever be told the same thing again. yt-dlp already knows the
    difference and says so with `expected=True`, so an expected failure
    logs one line and comes back as a plain 🚫 that nobody retries, while
    a real fault keeps its traceback and its retry.

    A login failure is a verdict however yt-dlp flagged it: another try
    with the same cookies gets the same answer, and the caller names the
    fix (`kind` says which failure it was).
    """
    kind = _classify(exc)
    if _expected(exc) or kind == 'auth':
        reason = _clean_error(exc)
        logging.info("resolver: %s — %s", url, reason)
        return ResolveError(reason, kind=kind)
    logging.warning("resolver: yt-dlp failed for %s", url, exc_info=True)
    return ResolveError(f"yt-dlp couldn't {action}: {_clean_error(exc)}",
                        emoji='❌', retryable=True, kind=kind)


class _YtdlpLogger:
    """Where yt-dlp's own output goes.

    `error` is deliberately not logged at ERROR level: every failure that
    reaches it is also raised, classified and logged by `resolve` itself,
    so an ERROR line here meant a tweet that simply has no video printed
    one. The message is kept — it is often more specific than the
    exception's — at WARNING, alongside yt-dlp's own warnings.
    """
    _log = logging.getLogger('yt_dlp')

    def debug(self, msg):
        self._log.debug(msg)

    def info(self, msg):
        self._log.info(msg)

    def warning(self, msg):
        self._log.warning(msg)

    def error(self, msg):
        self._log.warning(msg)

_extractors = None           # lazily built (compiles ~2000 URL regexes)


def available():
    """True when the yt-dlp module is importable."""
    return yt_dlp is not None


def version():
    return yt_dlp.version.__version__ if yt_dlp else None


def impersonation_available():
    """True when curl_cffi is installed — it gives yt-dlp a real Chrome
    TLS/HTTP fingerprint for sites that 403 non-browser clients."""
    if yt_dlp is None:
        return False
    try:
        import curl_cffi                     # noqa: F401
        return True
    except ImportError:
        return False


def _impersonate_target():
    from yt_dlp.networking.impersonate import ImpersonateTarget
    return ImpersonateTarget.from_str('chrome')


def _is_403(exc):
    text = str(exc)
    return 'HTTP Error 403' in text or '403: Forbidden' in text \
        or 'HTTPError 403' in text


def _matchers():
    global _extractors
    if _extractors is None:
        _extractors = [ie for ie in yt_dlp.extractor.gen_extractor_classes()
                       if ie.ie_key() != 'Generic' and ie.working()]
    return _extractors


def can_resolve(url):
    """True when a real (non-generic) yt-dlp extractor claims the URL.

    The generic extractor would "accept" any web page and then scrape it
    blind — exactly the article/dead-link noise the bot must not chase.
    """
    if yt_dlp is None:
        return False
    try:
        return any(ie.suitable(url) for ie in _matchers())
    except Exception:
        logging.warning("resolver: extractor matching failed for %s",
                        url, exc_info=True)
        return False


def filter_resolvable(urls):
    """The subset of `urls` worth handing to resolve(), order kept.
    First call compiles every extractor's URL regex — run in a thread."""
    return [u for u in dict.fromkeys(urls) if can_resolve(u)]


class Resolved:
    """A finished download: local file, display filename, source site, and
    whether the cookies it was given are what it downloaded with — false
    when there were none, or when YouTube only worked without them."""
    __slots__ = ('path', 'filename', 'site', 'used_cookies')

    def __init__(self, path, filename, site, used_cookies=False):
        self.path = path
        self.filename = filename
        self.site = site
        self.used_cookies = used_cookies


class Failure:
    """Why resolve_async() settled a link without a file: the reaction
    emoji, a reason fit to show the sender, and whether a later attempt
    could succeed (the Discord bot's retry queue reads it)."""
    __slots__ = ('emoji', 'reason', 'retryable')

    def __init__(self, emoji, reason, retryable=False):
        self.emoji = emoji
        self.reason = reason
        self.retryable = retryable


class Probe:
    """What a link holds, found out without downloading it."""
    __slots__ = ('title', 'duration', 'is_playlist', 'entry_count',
                 'has_video')

    def __init__(self, title=None, duration=None, is_playlist=False,
                 entry_count=0, has_video=False):
        self.title = title
        self.duration = duration
        self.is_playlist = is_playlist
        self.entry_count = entry_count
        self.has_video = has_video

    def has_media(self):
        """Worth a download: a real video format, or at least a duration
        (audio-only and some live pages report no format list)."""
        return bool(self.duration) or self.has_video


#: Hosts whose failures with a login are retried without it — see resolve.
_YOUTUBE_HOSTS = ('youtube.com', 'youtu.be', 'youtube-nocookie.com')


def _is_youtube(url):
    try:
        host = (urllib.parse.urlsplit(url).hostname or '').lower()
    except ValueError:
        return False
    return any(host == h or host.endswith('.' + h) for h in _YOUTUBE_HOSTS)


def _format_sort(quality):
    """yt-dlp's sort order for a quality cap ('1080', or 'best').

    A sort, not a filter: `[height<=1080]` would throw away every vertical
    video (a 1080p short is 1080x1920), and `res:1080` caps on the smaller
    side instead. `vcodec:h264` then picks the codec every browser plays
    over the VP9/AV1 a site prefers to serve, and `ext` the mp4 container.
    """
    res = 'res' if not quality or quality == 'best' else f'res:{quality}'
    return [res, 'vcodec:h264', 'ext']


def _session_opts(opts, cookies, pot_provider):
    """Add the login and PO-token settings resolve and probe share."""
    if cookies:
        if os.path.isfile(cookies):
            opts['cookiefile'] = cookies
        else:
            logging.warning("resolver: cookies file %s not found — ignoring",
                            cookies)
    if pot_provider:
        # The bgutil plugin's HTTP provider (bgutil-ytdlp-pot-provider in
        # the image, its server a sidecar container). Without a PO token a
        # server IP gets "Sign in to confirm you're not a bot" on most
        # YouTube videos.
        opts['extractor_args'] = {
            'youtubepot-bgutilhttp': {'base_url': [pot_provider]}}
    return opts


def _progress_hook(url, ctl=None):
    """Throttled per-stream progress lines to the container log — a
    multi-minute download must neither look like a hang nor log a line
    per chunk. 'finished' fires per stream (video, then audio).

    When `ctl` is given (a dict carrying `'stall'` seconds), the hook
    doubles as a stall watchdog: it resets a clock every time the byte
    count advances and raises DownloadStalled when no new bytes arrive
    within `'stall'` seconds. A healthy download — however large or slow
    — keeps advancing and never trips it; only a truly stuck transfer
    does. Raising here aborts yt-dlp's download loop from the inside, so
    the transfer actually stops instead of running on in an orphaned
    thread (contrast the old total-time cap)."""
    state = {'next': 0.0, 'advanced': None, 'bytes': -1}
    stall = ctl.get('stall') if ctl else None

    def hook(d):
        status = d.get('status')
        if status == 'downloading':
            now = time.monotonic()
            done = d.get('downloaded_bytes') or 0
            if state['advanced'] is None or done > state['bytes']:
                state['bytes'] = done
                state['advanced'] = now
            elif stall and now - state['advanced'] > stall:
                ctl['stalled'] = True
                raise DownloadStalled(
                    f"no progress for {stall:g}s at {done / 1e6:.1f} MB")
            if now < state['next']:
                return
            state['next'] = now + PROGRESS_INTERVAL
            logging.info(
                "resolver: %s — %s", url,
                parsing.format_progress(
                    done,
                    d.get('total_bytes') or d.get('total_bytes_estimate'),
                    d.get('speed'), d.get('eta')))
        elif status == 'finished':
            state['next'] = 0.0    # the next stream logs immediately
            state['advanced'] = None   # re-arm the watchdog for it
            state['bytes'] = -1
            total = d.get('total_bytes') or d.get('downloaded_bytes') or 0
            logging.info("resolver: %s — stream complete (%.1f MB)",
                         url, total / 1e6)
    return hook


def _postprocessor_hook(url):
    """The ffmpeg merge after both streams can take a while on big files;
    say so instead of going quiet."""
    def hook(d):
        if d.get('status') == 'started' and d.get('postprocessor') == 'Merger':
            logging.info("resolver: %s — merging video+audio streams", url)
    return hook


def resolve(url, tmpdir, max_bytes, cookies=None, stall_timeout=None, *,
            quality='best', pot_provider=None):
    """Download `url` via yt-dlp into `tmpdir`. Blocking — run in a
    thread. Returns a Resolved, or None when yt-dlp is not installed;
    every failure raises ResolveError.

    A 403 response is retried once impersonating Chrome (curl_cffi) —
    many sites block anything without a real browser's TLS fingerprint.
    `cookies` optionally names a Netscape cookies.txt with the site's
    login (yt-dlp writes rotated cookies back into it). A YouTube link
    that fails *with* a login, for a login or format reason, is tried once
    more without it: a stale or flagged session triggers the bot check and
    starves the format list, while the PO-token path works logged out.

    `quality` caps the resolution ('1080', or 'best'; see _format_sort)
    and `pot_provider` is the bgutil PO-token server's base URL.

    `stall_timeout` (seconds) aborts a download that stops advancing for
    that long, raising DownloadStalled — the transfer is killed from
    inside yt-dlp rather than left running. It watches *progress*, not
    total time, so big-but-healthy downloads are never cut off.
    """
    if yt_dlp is None:
        return None
    _sweep(tmpdir)
    token = uuid.uuid4().hex
    pattern = os.path.join(tmpdir, f'ytdlp-{token}.*')
    ctl = {'stall': stall_timeout, 'stalled': False}
    opts = {
        'outtmpl': os.path.join(tmpdir, f'ytdlp-{token}.%(ext)s'),
        # Best video+audio under the quality cap; ffmpeg (in the bot
        # image) merges into mp4 so the result always lands in the index's
        # allowed extensions.
        'format': 'bv*+ba/b',
        'format_sort': _format_sort(quality),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'playlist_items': '1',     # a bare playlist link ingests one item
        'max_filesize': max_bytes,
        'socket_timeout': 30,
        'retries': 2,
        # Progress comes from our hook; yt-dlp's own warnings/errors flow
        # through the logger so extraction trouble shows in the log too.
        'quiet': True,
        'noprogress': True,
        'logger': _YtdlpLogger(),
        'progress_hooks': [_progress_hook(url, ctl)],
        'postprocessor_hooks': [_postprocessor_hook(url)],
    }
    _session_opts(opts, cookies, pot_provider)
    used_cookies = 'cookiefile' in opts
    try:
        info = _download(url, opts, ctl, pattern, max_bytes, stall_timeout)
    except DownloadStalled:
        raise
    except ResolveError as e:
        if not (used_cookies and e.kind in ('auth', 'formats')
                and _is_youtube(url)):
            raise
        logging.info("resolver: %s — failed with the stored login (%s), "
                     "retrying without it", url, e.reason)
        used_cookies = False
        ctl['stalled'] = False
        opts = {k: v for k, v in opts.items() if k != 'cookiefile'}
        opts['progress_hooks'] = [_progress_hook(url, ctl)]
        info = _download(url, opts, ctl, pattern, max_bytes, stall_timeout)
    return _finish(url, info, pattern, max_bytes, used_cookies)


async def resolve_async(url, login, settings, tmpdir=None):
    """resolve() as both bots run it: in a thread, logged in with `login`
    (a site_logins.LoginRun, whose `used` it sets), under `settings` (an
    env.YtdlpSettings). Returns a Resolved, or a Failure carrying what to
    tell the sender; it never raises for a failed link, and it does the
    logging, so a bot only words its reply.

    `settings.max_duration` is a hard ceiling that only covers a resolve
    hanging during *extraction*: once bytes are moving, the stall
    watchdog takes over, and that watches progress rather than total
    time, so a large but healthy download is never cut off.
    """
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(resolve, url, tmpdir or tempfile.gettempdir(),
                              settings.max_bytes, login.path,
                              settings.stall_timeout,
                              quality=settings.quality,
                              pot_provider=settings.pot_provider),
            timeout=settings.max_duration)
    except asyncio.TimeoutError:
        logging.warning("resolver: exceeded the %gs hard ceiling for %s",
                        settings.max_duration, url)
        return Failure('❌', f"it hung for {settings.max_duration:g}s "
                             f"without downloading anything", retryable=True)
    except ResolveError as e:
        # Typed and specific (stalled, over the cap, blocked, no video
        # there, login required): worth repeating verbatim instead of "it
        # failed", with what an admin can do about a login.
        reason = e.reason + login.hint(e)
        logging.warning("resolver: %s — %s", url, reason)
        return Failure(e.emoji, reason, e.retryable)
    except Exception as e:
        logging.exception("resolver: unexpected failure for %s", url)
        return Failure('❌', f"unexpected error: {type(e).__name__} "
                             f"(see container log)")
    if res is None:      # yt-dlp missing; the bots check available() first
        return Failure('🚫', "the link resolver is unavailable")
    login.used = res.used_cookies
    return res


def _download(url, opts, ctl, pattern, max_bytes, stall_timeout):
    """One yt-dlp run of `opts`, plus its one impersonated retry on a 403.
    Returns yt-dlp's info dict; cleans up and raises ResolveError (or
    DownloadStalled) on failure."""
    def _run(run_opts):
        try:
            with yt_dlp.YoutubeDL(run_opts) as ydl:
                return ydl.extract_info(url, download=True) or {}
        except Exception as e:
            # yt-dlp may wrap the hook's DownloadStalled in its own error;
            # the ctl flag makes the stall unambiguous regardless.
            if ctl['stalled']:
                raise DownloadStalled(
                    f"no progress for {stall_timeout:g}s") from e
            raise

    logging.info("resolver: downloading %s (cap %.0f MB)",
                 url, max_bytes / 1e6)
    try:
        info = _run(opts)
    except DownloadStalled:
        logging.warning("resolver: %s — download stalled (no progress for "
                        "%gs), aborted", url, stall_timeout)
        _cleanup(pattern)
        raise
    except Exception as e:
        if not _is_403(e):
            _cleanup(pattern)
            raise _download_failure(url, e) from e
        if not impersonation_available():
            logging.warning(
                "resolver: %s returned HTTP 403 and curl_cffi is not "
                "installed — rebuild the bot image to enable browser "
                "impersonation", url)
            _cleanup(pattern)
            raise ResolveError(
                "blocked by the site (HTTP 403) and browser impersonation "
                "is unavailable — rebuild the bot image with curl_cffi",
                emoji='❌') from e
        logging.info("resolver: %s — HTTP 403, retrying with browser "
                     "impersonation", url)
        _cleanup(pattern)
        ctl['stalled'] = False
        try:
            info = _run({**opts, 'impersonate': _impersonate_target(),
                         'progress_hooks': [_progress_hook(url, ctl)]})
        except DownloadStalled:
            logging.warning("resolver: %s — download stalled (no progress "
                            "for %gs) after retry, aborted", url,
                            stall_timeout)
            _cleanup(pattern)
            raise
        except Exception as e2:
            logging.warning("resolver: impersonated retry failed for %s",
                            url, exc_info=True)
            _cleanup(pattern)
            raise ResolveError(
                f"still blocked (HTTP 403) after a browser-impersonation "
                f"retry: {_clean_error(e2)}", emoji='❌') from e2
    return info


def _finish(url, info, pattern, max_bytes, used_cookies):
    """The Resolved for a finished run, after checking what it produced."""
    if 'entries' in info:
        entries = [e for e in info.get('entries') or [] if e]
        info = entries[0] if entries else {}
    path = _final_path(info, pattern)
    if path is None:
        # Typical causes: max_filesize hit up front, or a page with no video.
        logging.info("resolver: nothing downloaded for %s", url)
        _cleanup(pattern)
        raise ResolveError(
            f"no downloadable video found (or it exceeds the "
            f"{max_bytes / 1e6:.0f} MB cap)")
    for extra in glob.glob(pattern):
        if extra != path:
            with contextlib.suppress(OSError):
                os.remove(extra)
    # max_filesize only guards formats whose size is known up front;
    # re-check what actually landed on disk.
    if os.path.getsize(path) > max_bytes:
        actual = os.path.getsize(path)
        logging.info("resolver: %s exceeds the size cap (%d bytes), dropping",
                     url, actual)
        _cleanup(pattern)
        raise ResolveError(
            f"the video ({actual / 1e6:.0f} MB) is larger than the "
            f"{max_bytes / 1e6:.0f} MB cap")
    site = (info.get('extractor_key') or '').lower() or None
    name = _display_name(info, path)
    logging.info("resolver: %s → %s (%.1f MB)",
                 url, name, os.path.getsize(path) / 1e6)
    return Resolved(path, name, site, used_cookies)


def probe(url, cookies=None, pot_provider=None):
    """What `url` holds, without downloading it. Blocking — run in a
    thread. Returns a Probe, or None when yt-dlp is not installed; a page
    yt-dlp can't read raises ResolveError.

    For links no real extractor claims, where only yt-dlp's generic
    extractor is left: it would "accept" any web page, so a download must
    not start on a hunch. One page fetch answers whether there is a video
    at all, and whether the link is a playlist.
    """
    if yt_dlp is None:
        return None
    opts = _session_opts({
        'quiet': True,
        'noprogress': True,
        'logger': _YtdlpLogger(),
        'skip_download': True,
        'noplaylist': True,
        # A playlist's entries are counted, not each extracted.
        'extract_flat': 'in_playlist',
        'socket_timeout': 30,
        'retries': 2,
    }, cookies, pot_provider)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as e:
        raise _download_failure(url, e, "read that page") from e
    if info.get('_type') in ('playlist', 'multi_video'):
        entries = info.get('entries')
        count = len(entries) if isinstance(entries, list) \
            else info.get('playlist_count') or 0
        return Probe(info.get('title'), None, True, count)
    has_video = any(f.get('vcodec') not in (None, 'none') and f.get('height')
                    for f in info.get('formats') or ())
    return Probe(info.get('title'), info.get('duration'), False, 0, has_video)


def _final_path(info, pattern):
    """The file yt-dlp actually produced for this resolve, or None.

    yt-dlp reports it in `requested_downloads` — for a merged download the
    last entry is the muxed output, not one of the source streams. The
    glob is only a fallback (older/fake yt-dlp): picking sorted(glob)[0]
    on its own is unsafe, because 'ytdlp-<t>.f137.mp4' sorts *before*
    'ytdlp-<t>.mp4', so a leftover video-only stream from a failed merge
    would win and be ingested silent.
    """
    for entry in reversed(info.get('requested_downloads') or []):
        path = entry.get('filepath') or entry.get('_filename')
        if path and not path.endswith(_PARTIAL_SUFFIXES) \
                and os.path.isfile(path):
            return path
    files = [f for f in sorted(glob.glob(pattern))
             if not f.endswith(_PARTIAL_SUFFIXES)]
    return files[0] if files else None


def _display_name(info, path):
    """Human filename for the index (it stores files by hash but keeps
    the original name for downloads): the video title, filesystem-safe."""
    ext = os.path.splitext(path)[1].lstrip('.')
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '', info.get('title') or '')
    title = re.sub(r'\s+', ' ', title).strip()[:120].rstrip('. ')
    return f"{title or info.get('id') or 'video'}.{ext}"


def _cleanup(pattern):
    for path in glob.glob(pattern):
        with contextlib.suppress(OSError):
            os.remove(path)


def _sweep(tmpdir, max_age=3600):
    """A resolve that timed out leaves its worker thread running and its
    partial file behind (threads can't be killed); reclaim anything old
    enough that no live resolve can still own it."""
    now = time.time()
    for path in glob.glob(os.path.join(tmpdir, 'ytdlp-*')):
        with contextlib.suppress(OSError):
            if now - os.path.getmtime(path) > max_age:
                os.remove(path)
