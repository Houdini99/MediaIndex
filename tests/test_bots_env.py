"""The bots' settings readers (bots_common/env.py).

Both bots read their configuration at import time and must fail fast: a
typo in a variable names itself at startup instead of leaving a bot that
silently ingests nothing. The readers look at os.environ when called, so
most cases set the variable and call them directly; the startup cases
import a bot's config in a fresh interpreter.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Config
from bots_common import env, parsing

ROOT = Path(__file__).resolve().parent.parent


def test_require_returns_the_value(monkeypatch):
    monkeypatch.setenv('X_TOKEN', 'abc')
    assert env.require('X_TOKEN') == 'abc'


@pytest.mark.parametrize('value', [None, ''])
def test_require_names_a_missing_variable(monkeypatch, value):
    if value is None:
        monkeypatch.delenv('X_TOKEN', raising=False)
    else:
        monkeypatch.setenv('X_TOKEN', value)
    with pytest.raises(RuntimeError, match='X_TOKEN environment variable is not set'):
        env.require('X_TOKEN')


@pytest.mark.parametrize('value, expected', [
    (None, 15.0), ('', 15.0), ('  ', 15.0), ('2.5', 2.5), (' 7 ', 7.0),
])
def test_num_falls_back_to_its_default(monkeypatch, value, expected):
    monkeypatch.delenv('X_WAIT', raising=False)
    if value is not None:
        monkeypatch.setenv('X_WAIT', value)
    assert env.num('X_WAIT', 15) == expected


def test_num_casts(monkeypatch):
    monkeypatch.setenv('X_COUNT', '3')
    assert env.num('X_COUNT', 5, int) == 3
    assert isinstance(env.num('X_MISSING_COUNT', 5, int), int)


@pytest.mark.parametrize('value, cast', [('abc', float), ('1.5', int), ('#logs', int)])
def test_a_typo_in_a_number_names_itself(monkeypatch, value, cast):
    monkeypatch.setenv('X_WAIT', value)
    with pytest.raises(RuntimeError, match=f"X_WAIT must be a number, got '{value}'"):
        env.num('X_WAIT', 15, cast)


def test_num_without_a_default_is_required(monkeypatch):
    monkeypatch.delenv('X_CHANNEL', raising=False)
    with pytest.raises(RuntimeError, match='X_CHANNEL environment variable is not set'):
        env.num('X_CHANNEL', cast=int)
    monkeypatch.setenv('X_CHANNEL', '42')
    assert env.num('X_CHANNEL', cast=int) == 42


@pytest.mark.parametrize('value, expected', [
    (None, True), ('', True), ('true', True), ('false', False),
    ('FALSE ', False), ('no', True), ('0', True),
])
def test_only_false_turns_off_a_switch_that_defaults_on(monkeypatch, value, expected):
    monkeypatch.delenv('X_SWITCH', raising=False)
    if value is not None:
        monkeypatch.setenv('X_SWITCH', value)
    assert env.flag('X_SWITCH', True) is expected


@pytest.mark.parametrize('value, expected', [
    (None, False), ('', False), ('true', True), (' True', True),
    ('false', False), ('yes', False), ('1', False),
])
def test_only_true_turns_on_a_switch_that_defaults_off(monkeypatch, value, expected):
    monkeypatch.delenv('X_SWITCH', raising=False)
    if value is not None:
        monkeypatch.setenv('X_SWITCH', value)
    assert env.flag('X_SWITCH', False) is expected


def _clear_ytdlp(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('DISCORD_YTDLP', 'TELEGRAM_YTDLP')):
            monkeypatch.delenv(name)


def test_ytdlp_defaults(monkeypatch):
    _clear_ytdlp(monkeypatch)
    assert env.ytdlp_settings('TELEGRAM') == env.YtdlpSettings(
        enabled=True, max_bytes=512_000_000, stall_timeout=120.0,
        max_duration=1800.0, cookies='', quality='1080', pot_provider='')


def test_ytdlp_reads_only_its_own_prefix(monkeypatch):
    _clear_ytdlp(monkeypatch)
    for name, value in (('TELEGRAM_YTDLP', 'false'),
                        ('TELEGRAM_YTDLP_MAX_MB', '1.5'),
                        ('TELEGRAM_YTDLP_STALL_TIMEOUT', '5'),
                        ('TELEGRAM_YTDLP_MAX_DURATION', '60'),
                        ('TELEGRAM_YTDLP_COOKIES', ' /run/cookies.txt '),
                        ('TELEGRAM_YTDLP_QUALITY', ' best '),
                        ('TELEGRAM_YTDLP_POT_PROVIDER', 'http://bgutil:4416/')):
        monkeypatch.setenv(name, value)

    assert env.ytdlp_settings('TELEGRAM') == env.YtdlpSettings(
        enabled=False, max_bytes=1_500_000, stall_timeout=5.0,
        max_duration=60.0, cookies='/run/cookies.txt', quality='best',
        pot_provider='http://bgutil:4416')
    assert env.ytdlp_settings('DISCORD') == env.ytdlp_settings('NOBODY')


@pytest.mark.parametrize('quality', ['4k', '360', 'Best'])
def test_an_unknown_quality_names_itself(monkeypatch, quality):
    monkeypatch.setenv('DISCORD_YTDLP_QUALITY', quality)
    with pytest.raises(RuntimeError, match=(
            'DISCORD_YTDLP_QUALITY must be one of '
            f"best/2160/1440/1080/720/480, got '{quality}'")):
        env.ytdlp_settings('DISCORD')


def test_the_bots_accept_what_the_index_accepts():
    """The bots cannot import app/, so their copy is pinned here: a type
    the index accepts but the bots skip would vanish without a word."""
    assert parsing.IMAGE_EXT == Config.ALLOWED_IMAGE_EXT | Config.ALLOWED_GIF_EXT
    assert parsing.ALLOWED_EXT == (Config.ALLOWED_IMAGE_EXT | Config.ALLOWED_GIF_EXT
                                   | Config.ALLOWED_VIDEO_EXT)


def _discord_log_channel(value):
    """DISCORD_LOG_CHANNEL_ID as a fresh import of the bot's config sees
    it: (exit code, value or error line)."""
    environ = dict(os.environ, DISCORD_LOG_CHANNEL_ID=value)
    out = subprocess.run(
        [sys.executable, '-c',
         'import discord_bot.config as c; print(c.LOG_CHANNEL_ID)'],
        cwd=ROOT, env=environ, capture_output=True, text=True)
    lines = (out.stdout or out.stderr).strip().splitlines()
    return out.returncode, lines[-1]


@pytest.mark.parametrize('value, expected', [('', '0'), (' 123 ', '123')])
def test_the_log_channel_is_optional(value, expected):
    assert _discord_log_channel(value) == (0, expected)


def test_a_log_channel_that_is_not_a_number_stops_the_bot():
    """It used to turn the channel off without a word."""
    code, line = _discord_log_channel('#bot-logs')
    assert code != 0
    assert line == ("RuntimeError: DISCORD_LOG_CHANNEL_ID must be a number, "
                    "got '#bot-logs'")
