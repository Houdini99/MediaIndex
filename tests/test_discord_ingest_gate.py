"""discord_bot: the Verified-role gate on ingest.

This is the boundary that keeps the *only* unauthenticated route into the
media pipeline closed. Everything downstream of it — yt-dlp in the bot
container, ffmpeg in the web container, which runs as root — processes
whatever bytes it is handed, so a regression here is a regression in the
one control standing in front of both.

`config.INGEST_ROLE` is read at import time, so every test rebinds it
explicitly rather than relying on the ambient environment.
"""
import asyncio
import types

import pytest

from discord_bot import bot as botmod
from discord_bot import config


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gate(monkeypatch):
    """Set INGEST_ROLE for one test."""
    def _set(value):
        monkeypatch.setattr(config, 'INGEST_ROLE', value)
    return _set


def role(rid, name):
    return types.SimpleNamespace(id=rid, name=name)


def member(*roles):
    return types.SimpleNamespace(id=99, roles=list(roles))


# ── config.author_is_verified ──────────────────────────────────────────────
def test_unset_role_allows_everyone(gate):
    """Empty = disabled, so an existing deployment that redeploys without
    the variable keeps ingesting instead of silently going dark."""
    gate('')
    assert config.author_is_verified(member()) is True
    assert config.author_is_verified(types.SimpleNamespace(id=1)) is True


def test_matches_by_role_name(gate):
    gate('Verified')
    assert config.author_is_verified(member(role(5, 'Verified'))) is True
    assert config.author_is_verified(member(role(5, 'Member'))) is False


def test_matches_by_role_id(gate):
    """A numeric value is an id, not a name — ids survive a role rename."""
    gate('12345')
    assert config.author_is_verified(member(role(12345, 'Verified'))) is True
    # Right name, wrong id: still refused when configured by id.
    assert config.author_is_verified(member(role(999, 'Verified'))) is False


def test_role_named_like_a_number_is_treated_as_an_id(gate):
    """Documents the one ambiguity: a role *named* '12345' cannot be matched
    by name, because a digits-only value is always read as an id."""
    gate('12345')
    assert config.author_is_verified(member(role(7, '12345'))) is False


def test_name_match_is_case_sensitive(gate):
    """Discord role names are case-sensitive; matching must not be looser
    than Discord itself, or 'verified' would grant the 'Verified' gate."""
    gate('Verified')
    assert config.author_is_verified(member(role(5, 'verified'))) is False


def test_member_with_several_roles(gate):
    gate('Verified')
    author = member(role(1, 'Member'), role(2, 'Verified'), role(3, 'Pings'))
    assert config.author_is_verified(author) is True


def test_author_without_roles_fails_closed(gate):
    """A User (DM) or webhook author has no .roles. The gate must refuse
    rather than fall through — it fails closed by design. (DM ingest
    passes the sender's fetched Member instead; see
    test_discord_dm_ingest.py.)"""
    gate('Verified')
    assert config.author_is_verified(types.SimpleNamespace(id=1)) is False
    assert config.author_is_verified(types.SimpleNamespace(id=1, roles=None)) is False
    assert config.author_is_verified(member()) is False


# ── on_message wiring ──────────────────────────────────────────────────────
class _Recorder:
    """Stands in for the bot: records whether _handle_message was reached."""

    _is_dm = staticmethod(botmod.IndexBot._is_dm)

    def __init__(self, channel_id):
        self.user = types.SimpleNamespace(id=1)
        self._channel_id = channel_id
        self.handled = []

    async def _handle_message(self, message, **kw):
        self.handled.append(message)


def message(author, channel_id, mid=500):
    return types.SimpleNamespace(
        id=mid, author=author,
        channel=types.SimpleNamespace(id=channel_id))


def deliver(author, gate_value, monkeypatch,
            channel_id=None, bot_user_id=1):
    """Run on_message for one message and report whether it was ingested."""
    monkeypatch.setattr(config, 'INGEST_ROLE', gate_value)
    chan = config.INGEST_CHANNEL_ID if channel_id is None else channel_id
    rec = _Recorder(chan)
    msg = message(author, chan)
    run(botmod.IndexBot.on_message(rec, msg))
    return bool(rec.handled)


def test_verified_author_is_ingested(monkeypatch):
    assert deliver(member(role(5, 'Verified')), 'Verified', monkeypatch)


def test_unverified_author_is_dropped(monkeypatch):
    assert not deliver(member(role(5, 'Member')), 'Verified', monkeypatch)


def test_gate_disabled_lets_anyone_through(monkeypatch):
    assert deliver(member(), '', monkeypatch)


def test_wrong_channel_still_short_circuits_first(monkeypatch):
    """The channel check runs before the role check, so a Verified member
    posting elsewhere is still ignored."""
    assert not deliver(member(role(5, 'Verified')), 'Verified', monkeypatch,
                       channel_id=config.INGEST_CHANNEL_ID + 1)


def test_bots_own_message_is_ignored_even_when_verified(monkeypatch):
    """The self-loop guard must stay ahead of the gate: the bot account may
    well hold the role itself."""
    monkeypatch.setattr(config, 'INGEST_ROLE', 'Verified')
    rec = _Recorder(config.INGEST_CHANNEL_ID)
    own = types.SimpleNamespace(id=rec.user.id, roles=[role(5, 'Verified')])
    run(botmod.IndexBot.on_message(
        rec, message(own, config.INGEST_CHANNEL_ID)))
    assert not rec.handled
