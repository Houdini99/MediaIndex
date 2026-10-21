"""bots_common/resolver.py: extractor matching, the download wrapper's
failure contract (every expected failure raises a typed ResolveError with
a human reason; the resolver never lets a raw yt-dlp error escape), and
filename/site derivation. yt-dlp itself is faked for the download paths so
the tests run offline; the matching tests use the real extractor list when
the library is installed."""
import logging
import os
import time
import types

import pytest

from bots_common import resolver


@pytest.fixture
def no_ytdlp(monkeypatch):
    monkeypatch.setattr(resolver, 'yt_dlp', None)


def _fake_ytdlp(monkeypatch, info=None, raise_error=None, write_ext='mp4',
                write_bytes=b'x' * 100):
    """Install a stand-in yt_dlp module whose YoutubeDL 'downloads' by
    writing a file where the outtmpl points."""
    calls = {}

    class FakeYDL:
        def __init__(self, opts):
            calls['opts'] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            calls['url'] = url
            if raise_error is not None:
                raise raise_error
            if write_ext is not None:
                path = calls['opts']['outtmpl'] % {'ext': write_ext}
                with open(path, 'wb') as fh:
                    fh.write(write_bytes)
                calls['path'] = path
            return info if info is not None else {}

    monkeypatch.setattr(resolver, 'yt_dlp',
                        types.SimpleNamespace(YoutubeDL=FakeYDL))
    return calls


# ── availability / matching ────────────────────────────────────────────────

def test_unavailable_without_ytdlp(no_ytdlp):
    assert not resolver.available()
    assert resolver.version() is None
    assert not resolver.can_resolve('https://www.youtube.com/watch?v=x')
    assert resolver.filter_resolvable(['https://www.youtube.com/watch?v=x']) == []
    assert resolver.resolve('https://www.youtube.com/watch?v=x',
                            '/tmp', 10**6) is None


def test_platform_urls_match_real_extractors():
    pytest.importorskip('yt_dlp')
    assert resolver.can_resolve('https://www.youtube.com/watch?v=AAAAAAAAAAA')
    assert resolver.can_resolve('https://youtu.be/AAAAAAAAAAA')
    assert resolver.can_resolve(
        'https://www.tiktok.com/@user/video/7000000000000000000')


def test_plain_pages_do_not_match():
    """Article/generic links must stay with the bot's 🚫 fallback — only
    the generic extractor would claim them, and it is excluded."""
    pytest.importorskip('yt_dlp')
    assert not resolver.can_resolve('https://example.com/some/article')
    assert not resolver.can_resolve('https://example.com/cat.gif')


def test_filter_resolvable_dedupes_and_keeps_order():
    pytest.importorskip('yt_dlp')
    yt = 'https://www.youtube.com/watch?v=AAAAAAAAAAA'
    urls = ['https://example.com/article', yt, yt]
    assert resolver.filter_resolvable(urls) == [yt]


# ── resolve() download wrapper ─────────────────────────────────────────────

def test_resolve_happy_path(tmp_path, monkeypatch):
    calls = _fake_ytdlp(monkeypatch,
                        info={'title': 'Cat Video', 'id': 'abc123',
                              'extractor_key': 'Youtube'})
    res = resolver.resolve('https://www.youtube.com/watch?v=abc123',
                           str(tmp_path), 10**6)
    assert res is not None
    assert res.path == calls['path']
    assert os.path.isfile(res.path)
    assert res.filename == 'Cat Video.mp4'
    assert res.site == 'youtube'
    assert calls['opts']['max_filesize'] == 10**6
    assert calls['opts']['noplaylist'] is True


def test_resolve_picks_first_playlist_entry(tmp_path, monkeypatch):
    _fake_ytdlp(monkeypatch,
                info={'entries': [None,
                                  {'title': 'First', 'extractor_key': 'TikTok'}]})
    res = resolver.resolve('u', str(tmp_path), 10**6)
    assert res.filename == 'First.mp4'
    assert res.site == 'tiktok'


def test_resolve_wraps_download_errors(tmp_path, monkeypatch):
    _fake_ytdlp(monkeypatch, raise_error=RuntimeError('boom'), write_ext=None)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert 'boom' in exc.value.reason and exc.value.emoji == '❌'
    assert list(tmp_path.iterdir()) == []


def test_resolve_raises_when_nothing_downloaded(tmp_path, monkeypatch):
    """max_filesize hit or a page without video: extract_info succeeds
    but no file lands on disk."""
    _fake_ytdlp(monkeypatch, write_ext=None)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert 'no downloadable video' in exc.value.reason


def test_resolve_drops_oversized_download(tmp_path, monkeypatch):
    """max_filesize only guards formats with a known size up front — an
    unknown-size download that lands too big is deleted, not ingested."""
    _fake_ytdlp(monkeypatch, write_bytes=b'x' * 200)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), max_bytes=100)
    assert 'cap' in exc.value.reason
    assert list(tmp_path.iterdir()) == []


def test_resolve_ignores_partial_files(tmp_path, monkeypatch):
    _fake_ytdlp(monkeypatch, write_ext='mp4.part')
    with pytest.raises(resolver.ResolveError):
        resolver.resolve('u', str(tmp_path), 10**6)


def test_sweep_reclaims_only_stale_files(tmp_path, monkeypatch):
    stale = tmp_path / 'ytdlp-dead.mp4.part'
    stale.write_bytes(b'x')
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    fresh = tmp_path / 'ytdlp-live.mp4'
    fresh.write_bytes(b'x')
    _fake_ytdlp(monkeypatch)
    resolver.resolve('u', str(tmp_path), 10**6)
    assert not stale.exists()
    assert fresh.exists()


# ── display names ──────────────────────────────────────────────────────────

def test_display_name_sanitizes_and_truncates():
    info = {'title': 'a/b\\c: "d" <e>|f?*', 'id': 'vid1'}
    assert resolver._display_name(info, '/tmp/x.mp4') == 'abc d ef.mp4'
    info = {'title': 'y' * 300}
    assert resolver._display_name(info, '/tmp/x.webm') == 'y' * 120 + '.webm'


def test_display_name_falls_back_to_id_then_generic():
    assert resolver._display_name({'id': 'abc'}, '/t/x.mp4') == 'abc.mp4'
    assert resolver._display_name({}, '/t/x.mp4') == 'video.mp4'


# ── container-log progress hooks ───────────────────────────────────────────

def test_progress_hook_logs_and_throttles(caplog):
    import logging
    hook = resolver._progress_hook('http://u')
    with caplog.at_level(logging.INFO):
        hook({'status': 'downloading', 'downloaded_bytes': 1_000_000,
              'total_bytes': 4_000_000, 'speed': 2_000_000, 'eta': 2})
        hook({'status': 'downloading', 'downloaded_bytes': 2_000_000,
              'total_bytes': 4_000_000})   # inside the throttle window
        hook({'status': 'finished', 'total_bytes': 4_000_000})
    msgs = [r.message for r in caplog.records]
    assert sum('(25%)' in m for m in msgs) == 1
    assert not any('(50%)' in m for m in msgs)
    assert any('stream complete (4.0 MB)' in m for m in msgs)


def test_progress_hook_no_watchdog_without_ctl(monkeypatch):
    # No ctl → the hook is a pure logger; a frozen byte count never raises.
    clock = {'t': 0.0}
    monkeypatch.setattr(resolver.time, 'monotonic', lambda: clock['t'])
    hook = resolver._progress_hook('http://u')
    hook({'status': 'downloading', 'downloaded_bytes': 1})
    clock['t'] += 100_000
    hook({'status': 'downloading', 'downloaded_bytes': 1})   # must not raise


def test_progress_hook_stall_watchdog(monkeypatch):
    clock = {'t': 1000.0}
    monkeypatch.setattr(resolver.time, 'monotonic', lambda: clock['t'])
    ctl = {'stall': 30, 'stalled': False}
    hook = resolver._progress_hook('http://u', ctl)
    hook({'status': 'downloading', 'downloaded_bytes': 1_000})   # arms it
    clock['t'] = 5000.0                                          # long gap…
    hook({'status': 'downloading', 'downloaded_bytes': 2_000})   # …but advanced
    clock['t'] = 5020.0                                          # frozen, but
    hook({'status': 'downloading', 'downloaded_bytes': 2_000})   # within window
    assert not ctl['stalled']
    clock['t'] = 5060.0                                          # frozen > 30s
    with pytest.raises(resolver.DownloadStalled):
        hook({'status': 'downloading', 'downloaded_bytes': 2_000})
    assert ctl['stalled']


def test_progress_hook_finished_rearms_watchdog(monkeypatch):
    # 'finished' (end of the video stream) must reset the clock so the audio
    # stream's small initial byte count isn't read as a stall.
    clock = {'t': 0.0}
    monkeypatch.setattr(resolver.time, 'monotonic', lambda: clock['t'])
    ctl = {'stall': 30, 'stalled': False}
    hook = resolver._progress_hook('http://u', ctl)
    hook({'status': 'downloading', 'downloaded_bytes': 9_000})
    clock['t'] = 100.0
    hook({'status': 'finished', 'total_bytes': 9_000})
    clock['t'] = 120.0                                    # 120s after stream 1
    hook({'status': 'downloading', 'downloaded_bytes': 500})   # new stream
    assert not ctl['stalled']


def test_resolve_reraises_stall(monkeypatch, tmp_path):
    clock = {'t': 0.0}
    monkeypatch.setattr(resolver.time, 'monotonic', lambda: clock['t'])

    class StallingYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def extract_info(self, url, download=True):
            hook = self.opts['progress_hooks'][0]
            hook({'status': 'downloading', 'downloaded_bytes': 1_000})
            clock['t'] = 10_000.0
            # yt-dlp typically wraps a hook exception in its own error; the
            # ctl flag must still surface it as DownloadStalled.
            try:
                hook({'status': 'downloading', 'downloaded_bytes': 1_000})
            except resolver.DownloadStalled as e:
                raise RuntimeError("wrapped by yt-dlp") from e
            return {}

    monkeypatch.setattr(resolver, 'yt_dlp',
                        types.SimpleNamespace(YoutubeDL=StallingYDL))
    with pytest.raises(resolver.DownloadStalled):
        resolver.resolve('u', str(tmp_path), 10**6, stall_timeout=30)


def test_postprocessor_hook_only_reports_merges(caplog):
    import logging
    hook = resolver._postprocessor_hook('http://u')
    with caplog.at_level(logging.INFO):
        hook({'status': 'started', 'postprocessor': 'MoveFiles'})
        hook({'status': 'started', 'postprocessor': 'Merger'})
        hook({'status': 'finished', 'postprocessor': 'Merger'})
    msgs = [r.message for r in caplog.records]
    assert msgs == ['resolver: http://u — merging video+audio streams']


# ── 403 → browser-impersonation retry ──────────────────────────────────────

def _fake_ytdlp_403(monkeypatch, then_succeed=True):
    """First extract_info call raises a 403; later calls succeed (or keep
    failing). Returns the list of opts each YoutubeDL saw."""
    opts_seen = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            opts_seen.append(self.opts)
            if len(opts_seen) == 1 or not then_succeed:
                raise RuntimeError(
                    'ERROR: [Site] x: Unable to download webpage: '
                    'HTTP Error 403: Forbidden')
            path = self.opts['outtmpl'] % {'ext': 'mp4'}
            with open(path, 'wb') as fh:
                fh.write(b'x' * 50)
            return {'title': 'Recovered', 'extractor_key': 'SomeSite'}

    monkeypatch.setattr(resolver, 'yt_dlp',
                        types.SimpleNamespace(YoutubeDL=FakeYDL))
    return opts_seen


def test_403_retries_with_impersonation(tmp_path, monkeypatch):
    opts_seen = _fake_ytdlp_403(monkeypatch)
    monkeypatch.setattr(resolver, 'impersonation_available', lambda: True)
    monkeypatch.setattr(resolver, '_impersonate_target', lambda: 'chrome-t')
    res = resolver.resolve('u', str(tmp_path), 10**6)
    assert res is not None and res.filename == 'Recovered.mp4'
    assert 'impersonate' not in opts_seen[0]
    assert opts_seen[1]['impersonate'] == 'chrome-t'


def test_403_retry_failure_still_falls_back(tmp_path, monkeypatch):
    opts_seen = _fake_ytdlp_403(monkeypatch, then_succeed=False)
    monkeypatch.setattr(resolver, 'impersonation_available', lambda: True)
    monkeypatch.setattr(resolver, '_impersonate_target', lambda: 'chrome-t')
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert '403' in exc.value.reason and exc.value.emoji == '❌'
    assert len(opts_seen) == 2          # exactly one retry, no loop
    assert list(tmp_path.iterdir()) == []


def test_403_without_curl_cffi_does_not_retry(tmp_path, monkeypatch):
    opts_seen = _fake_ytdlp_403(monkeypatch)
    monkeypatch.setattr(resolver, 'impersonation_available', lambda: False)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert 'curl_cffi' in exc.value.reason
    assert len(opts_seen) == 1


def test_non_403_error_does_not_retry(tmp_path, monkeypatch):
    _fake_ytdlp(monkeypatch, raise_error=RuntimeError('HTTP Error 500'),
                write_ext=None)
    monkeypatch.setattr(resolver, 'impersonation_available', lambda: True)
    with pytest.raises(resolver.ResolveError):
        resolver.resolve('u', str(tmp_path), 10**6)


# ── picking the finished file ──────────────────────────────────────────────

def _fake_ytdlp_files(monkeypatch, exts, report=None):
    """Fake yt-dlp writing one file per ext, and optionally reporting one
    of them in `requested_downloads` the way the real one does."""
    calls = {}

    class FakeYDL:
        def __init__(self, opts):
            calls['opts'] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            paths = []
            for ext in exts:
                path = calls['opts']['outtmpl'] % {'ext': ext}
                with open(path, 'wb') as fh:
                    fh.write(b'x' * 100)
                paths.append(path)
            calls['paths'] = paths
            info = {'title': 'Clip', 'extractor_key': 'YouTube'}
            if report is not None:
                info['requested_downloads'] = [{'filepath': paths[report]}]
            return info

    monkeypatch.setattr(resolver, 'yt_dlp',
                        types.SimpleNamespace(YoutubeDL=FakeYDL))
    return calls


def test_resolve_prefers_the_merged_output_over_a_leftover_stream(
        tmp_path, monkeypatch):
    """A merge that leaves its source streams behind used to win the pick:
    'ytdlp-<t>.f137.mp4' sorts before 'ytdlp-<t>.mp4', so sorted(glob)[0]
    handed the video-only stream to the index — a silent video."""
    calls = _fake_ytdlp_files(monkeypatch, ['f137.mp4', 'mp4'], report=1)
    res = resolver.resolve('u', str(tmp_path), 10**6)
    assert res.path == calls['paths'][1]
    assert '.f137.' not in res.path
    # the leftover stream is cleaned up, not ingested and not left lying
    assert [p.name for p in tmp_path.iterdir()] == [os.path.basename(res.path)]


def test_resolve_falls_back_to_the_glob_without_requested_downloads(
        tmp_path, monkeypatch):
    """Older yt-dlp (and the other fakes here) report no requested_downloads
    — the glob stays the fallback."""
    calls = _fake_ytdlp_files(monkeypatch, ['mp4'], report=None)
    res = resolver.resolve('u', str(tmp_path), 10**6)
    assert res.path == calls['paths'][0]


def test_final_path_skips_partial_and_missing_entries(tmp_path):
    real = tmp_path / 'ytdlp-t.mp4'
    real.write_bytes(b'x')
    part = tmp_path / 'ytdlp-t.mp4.part'
    part.write_bytes(b'x')
    info = {'requested_downloads': [
        {'filepath': str(real)},
        {'filepath': str(tmp_path / 'never-written.mp4')},
        {'filepath': str(part)},
    ]}
    assert resolver._final_path(
        info, str(tmp_path / 'ytdlp-t.*')) == str(real)


def test_final_path_returns_none_when_nothing_landed(tmp_path):
    assert resolver._final_path({}, str(tmp_path / 'ytdlp-t.*')) is None


# ── retryability ───────────────────────────────────────────────────────────

def test_transient_failures_are_retryable(tmp_path, monkeypatch):
    """The bot's retry queue reads this flag: a failed transfer is worth
    another attempt, a settled verdict is not."""
    _fake_ytdlp(monkeypatch, raise_error=RuntimeError('boom'), write_ext=None)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert exc.value.retryable is True
    assert resolver.DownloadStalled('no progress').retryable is True


def test_settled_verdicts_are_not_retryable(tmp_path, monkeypatch):
    _fake_ytdlp(monkeypatch, write_ext=None)
    with pytest.raises(resolver.ResolveError) as no_video:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert no_video.value.retryable is False

    _fake_ytdlp(monkeypatch, write_bytes=b'x' * 200)
    with pytest.raises(resolver.ResolveError) as too_big:
        resolver.resolve('u', str(tmp_path), max_bytes=100)
    assert too_big.value.retryable is False

    _fake_ytdlp(monkeypatch, raise_error=RuntimeError('HTTP Error 403'),
                write_ext=None)
    monkeypatch.setattr(resolver, 'impersonation_available', lambda: False)
    with pytest.raises(resolver.ResolveError) as blocked:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert blocked.value.retryable is False


# ── cookies ────────────────────────────────────────────────────────────────

def test_cookies_file_passed_through(tmp_path, monkeypatch):
    calls = _fake_ytdlp(monkeypatch)
    jar = tmp_path / 'cookies.txt'
    jar.write_text('# Netscape HTTP Cookie File\n')
    resolver.resolve('u', str(tmp_path), 10**6, cookies=str(jar))
    assert calls['opts']['cookiefile'] == str(jar)


def test_missing_cookies_file_ignored(tmp_path, monkeypatch):
    calls = _fake_ytdlp(monkeypatch)
    res = resolver.resolve('u', str(tmp_path), 10**6,
                           cookies=str(tmp_path / 'nope.txt'))
    assert res is not None
    assert 'cookiefile' not in calls['opts']


# ── expected failures vs real ones ─────────────────────────────────────────
# yt-dlp marks the failures that are *answers* — 'No video could be found
# in this tweet', 'Video unavailable', a geo block — with expected=True.
# Reporting those like a broken transfer cost a 30-line traceback per
# posted tweet and a retry that could only be told the same thing again.

class _ExtractorError(Exception):
    """Shaped like yt_dlp.utils.ExtractorError: it carries the flag."""

    def __init__(self, msg, expected=False):
        super().__init__(msg)
        self.expected = expected


class _DownloadError(Exception):
    """Shaped like yt_dlp.utils.DownloadError: YoutubeDL catches the
    ExtractorError and re-raises this, keeping the original in exc_info."""

    def __init__(self, msg, cause):
        super().__init__(msg)
        self.exc_info = (type(cause), cause, None)


def _twitter_no_video():
    inner = _ExtractorError(
        '[twitter] 1000000000000000001: No video could be found in this '
        'tweet', expected=True)
    return _DownloadError(
        'ERROR: [twitter] 1000000000000000001: No video could be found in '
        'this tweet', inner)


def test_an_expected_failure_is_a_settled_verdict(tmp_path, monkeypatch):
    _fake_ytdlp(monkeypatch, raise_error=_twitter_no_video(), write_ext=None)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert exc.value.emoji == '🚫'
    assert exc.value.retryable is False, "retrying cannot change the answer"


def test_an_expected_failure_keeps_the_sites_own_wording(tmp_path,
                                                         monkeypatch):
    """It is more specific than anything this module could say."""
    _fake_ytdlp(monkeypatch, raise_error=_twitter_no_video(), write_ext=None)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert exc.value.reason == 'No video could be found in this tweet', \
        "yt-dlp's console prefix ('ERROR: [twitter] <id>:') is not a reason"


def test_an_expected_failure_does_not_log_a_traceback(tmp_path, monkeypatch,
                                                      caplog):
    _fake_ytdlp(monkeypatch, raise_error=_twitter_no_video(), write_ext=None)
    with caplog.at_level(logging.DEBUG), \
            pytest.raises(resolver.ResolveError):
        resolver.resolve('u', str(tmp_path), 10**6)
    failures = [r for r in caplog.records if r.name == 'root' and r.exc_info]
    assert failures == [], "a tweet with no video is not a stack trace"


def test_an_unexpected_failure_still_gets_its_traceback(tmp_path, monkeypatch,
                                                        caplog):
    """The flag must not become a way to hide real breakage."""
    _fake_ytdlp(monkeypatch, raise_error=RuntimeError('boom'), write_ext=None)
    with caplog.at_level(logging.DEBUG), \
            pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert exc.value.emoji == '❌' and exc.value.retryable is True
    assert any(r.exc_info for r in caplog.records)


def test_a_bare_expected_extractor_error_counts_too(tmp_path, monkeypatch):
    """Not every path wraps it in a DownloadError."""
    _fake_ytdlp(monkeypatch, write_ext=None,
                raise_error=_ExtractorError('Private video', expected=True))
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert exc.value.reason == 'Private video' and exc.value.emoji == '🚫'


def test_ytdlps_own_errors_are_not_logged_as_errors(caplog):
    """resolve() logs the classified verdict itself, so an ERROR line here
    only ever double-reported it — at the wrong level for a tweet that
    simply has no video."""
    with caplog.at_level(logging.DEBUG):
        resolver._YtdlpLogger().error('ERROR: [twitter] x: no video')
    levels = {r.levelno for r in caplog.records if r.name == 'yt_dlp'}
    assert levels == {logging.WARNING}


@pytest.mark.parametrize('text,expected', [
    ('ERROR: [twitter] 123: No video could be found', 'No video could be found'),
    ('ERROR: [youtube] abc: Private video. Sign in.', 'Private video. Sign in.'),
    ('ERROR: unable to download video data: HTTP Error 404',
     'unable to download video data: HTTP Error 404'),
    ('boom', 'boom'),
])
def test_the_console_prefix_is_stripped_without_eating_the_message(text,
                                                                   expected):
    assert resolver._clean_error(Exception(text)) == expected


# ── Logins, PO tokens and the quality cap ──────────────────────────────────
# The site's login, a PO-token provider for YouTube, and a cap that keeps a
# 4K VP9 master from blowing the size limit.

def test_quality_caps_the_resolution_and_prefers_h264(tmp_path, monkeypatch):
    calls = _fake_ytdlp(monkeypatch)
    resolver.resolve('u', str(tmp_path), 10**6, quality='1080')
    assert calls['opts']['format_sort'] == ['res:1080', 'vcodec:h264', 'ext']


def test_best_quality_still_prefers_h264_at_each_height(tmp_path,
                                                        monkeypatch):
    calls = _fake_ytdlp(monkeypatch)
    resolver.resolve('u', str(tmp_path), 10**6)
    assert calls['opts']['format_sort'] == ['res', 'vcodec:h264', 'ext']


def test_the_po_token_provider_is_handed_to_the_plugin(tmp_path, monkeypatch):
    calls = _fake_ytdlp(monkeypatch)
    resolver.resolve('u', str(tmp_path), 10**6,
                     pot_provider='http://bgutil:4416')
    assert calls['opts']['extractor_args'] == {
        'youtubepot-bgutilhttp': {'base_url': ['http://bgutil:4416']}}


def test_no_po_token_provider_sets_no_extractor_args(tmp_path, monkeypatch):
    calls = _fake_ytdlp(monkeypatch)
    resolver.resolve('u', str(tmp_path), 10**6)
    assert 'extractor_args' not in calls['opts']


def test_a_download_with_cookies_reports_it_used_them(tmp_path, monkeypatch):
    _fake_ytdlp(monkeypatch)
    jar = tmp_path / 'cookies.txt'
    jar.write_text('# Netscape HTTP Cookie File\n')
    assert resolver.resolve('u', str(tmp_path), 10**6,
                            cookies=str(jar)).used_cookies is True
    assert resolver.resolve('u', str(tmp_path), 10**6).used_cookies is False


@pytest.mark.parametrize('message', [
    "Sign in to confirm you're not a bot. Use --cookies-from-browser",
    'This video is private',
    'Requested content is not available, rate-limit reached or login '
    'required',
])
def test_a_login_failure_is_a_verdict_with_its_kind(tmp_path, monkeypatch,
                                                    message):
    """Even when yt-dlp didn't flag it expected: the same cookies get the
    same answer, and the bot names the fix (an admin uploading a login)."""
    _fake_ytdlp(monkeypatch, raise_error=RuntimeError(message),
                write_ext=None)
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('u', str(tmp_path), 10**6)
    assert exc.value.kind == 'auth'
    assert exc.value.emoji == '🚫' and exc.value.retryable is False


def _fake_login_sensitive_ytdlp(monkeypatch, failure):
    """A yt-dlp that fails with `failure` whenever it is given cookies and
    downloads fine without them — a stale YouTube session."""
    runs = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            runs.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            if 'cookiefile' in self.opts:
                raise RuntimeError(failure)
            path = self.opts['outtmpl'] % {'ext': 'mp4'}
            with open(path, 'wb') as fh:
                fh.write(b'x' * 10)
            return {'title': 'Clip', 'extractor_key': 'Youtube'}

    monkeypatch.setattr(resolver, 'yt_dlp',
                        types.SimpleNamespace(YoutubeDL=FakeYDL))
    return runs


@pytest.mark.parametrize('failure', [
    "Sign in to confirm you're not a bot",
    'Requested format is not available',
])
def test_youtube_is_retried_without_a_login_that_fails(tmp_path, monkeypatch,
                                                       failure):
    """A stale or flagged session triggers the bot check and starves the
    format list, while the PO-token path works logged out."""
    runs = _fake_login_sensitive_ytdlp(monkeypatch, failure)
    jar = tmp_path / 'cookies.txt'
    jar.write_text('# Netscape HTTP Cookie File\n')

    res = resolver.resolve('https://www.youtube.com/watch?v=1', str(tmp_path),
                           10**6, cookies=str(jar))

    assert [('cookiefile' in r) for r in runs] == [True, False]
    assert res.used_cookies is False, \
        "the jar from the failed run must not be written back"


def test_other_sites_are_not_retried_without_their_login(tmp_path,
                                                         monkeypatch):
    runs = _fake_login_sensitive_ytdlp(monkeypatch, 'login required')
    jar = tmp_path / 'cookies.txt'
    jar.write_text('# Netscape HTTP Cookie File\n')
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.resolve('https://www.instagram.com/reel/1/', str(tmp_path),
                         10**6, cookies=str(jar))
    assert len(runs) == 1 and exc.value.kind == 'auth'


def test_youtube_without_a_login_is_not_retried(tmp_path, monkeypatch):
    runs = []

    class FakeYDL:
        def __init__(self, opts):
            runs.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(resolver, 'yt_dlp',
                        types.SimpleNamespace(YoutubeDL=FakeYDL))
    with pytest.raises(resolver.ResolveError):
        resolver.resolve('https://youtu.be/1', str(tmp_path), 10**6)
    assert len(runs) == 1


# ── probe() ────────────────────────────────────────────────────────────────

def _fake_probe(monkeypatch, info=None, raise_error=None):
    calls = {}

    class FakeYDL:
        def __init__(self, opts):
            calls['opts'] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            calls['download'] = download
            if raise_error is not None:
                raise raise_error
            return info

    monkeypatch.setattr(resolver, 'yt_dlp',
                        types.SimpleNamespace(YoutubeDL=FakeYDL))
    return calls


def test_probe_downloads_nothing(monkeypatch):
    calls = _fake_probe(monkeypatch, info={'title': 'x'})
    resolver.probe('u', pot_provider='http://bgutil:4416')
    assert calls['download'] is False
    assert calls['opts']['skip_download'] is True
    assert 'youtubepot-bgutilhttp' in calls['opts']['extractor_args']


def test_probe_sees_a_real_video_format(monkeypatch):
    _fake_probe(monkeypatch, info={'title': 'Clip', 'formats': [
        {'vcodec': 'none', 'height': None},
        {'vcodec': 'avc1', 'height': 720}]})
    assert resolver.probe('u').has_media() is True


def test_probe_finds_no_media_on_an_article(monkeypatch):
    _fake_probe(monkeypatch, info={'title': 'An article', 'formats': [
        {'vcodec': 'none', 'height': None}]})
    assert resolver.probe('u').has_media() is False


def test_probe_counts_a_playlist(monkeypatch):
    _fake_probe(monkeypatch, info={'_type': 'playlist',
                                   'entries': [{}, {}, {}]})
    meta = resolver.probe('u')
    assert meta.is_playlist and meta.entry_count == 3


def test_probe_reports_an_unreadable_page_as_a_verdict(monkeypatch):
    _fake_probe(monkeypatch, raise_error=_ExtractorError(
        'Unsupported URL: https://example.com/a', expected=True))
    with pytest.raises(resolver.ResolveError) as exc:
        resolver.probe('https://example.com/a')
    assert exc.value.emoji == '🚫' and 'Unsupported URL' in exc.value.reason


def test_probe_without_ytdlp_is_none(no_ytdlp):
    assert resolver.probe('u') is None
