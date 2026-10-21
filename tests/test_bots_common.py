"""Both bots run one copy of the shared code in bots_common/.

The bots used to build from their own directories, so each carried a
byte-for-byte copy of the yt-dlp resolver and of the tag/link parsers, and
this file diffed the copies to keep them from drifting. Drift there is
invisible — a caption that tags on one bot and is prose on the other looks
like user error, not a bug. Both images now build from the repository root
and import bots_common, so what is pinned here is that nobody brings a
private copy back.
"""
import pathlib

import pytest

import bots_common.parsing as common_parsing
import bots_common.resolver as common_resolver
from discord_bot import download as discord_download
from discord_bot import parsing as discord_parsing
from telegram_bot import download as telegram_download
from telegram_bot import parsing as telegram_parsing

REPO = pathlib.Path(__file__).resolve().parent.parent

SHARED = ('extract_urls', 'strip_urls', 'media_url_info',
          'parse_message_tags', 'format_progress')


@pytest.mark.parametrize('name', SHARED)
def test_both_bots_parse_with_the_shared_code(name):
    shared = getattr(common_parsing, name)
    assert getattr(discord_parsing, name) is shared
    assert getattr(telegram_parsing, name) is shared


def test_both_bots_resolve_with_the_shared_resolver():
    assert discord_download.resolver is common_resolver
    assert telegram_download.resolver is common_resolver


def test_no_private_copies_came_back():
    for bot in ('discord_bot', 'telegram_bot'):
        assert not (REPO / bot / 'resolver.py').exists(), (
            f"{bot}/resolver.py is back — import bots_common.resolver instead")
        for name in SHARED:
            src = (REPO / bot / 'parsing.py').read_text()
            assert f'\ndef {name}(' not in src, (
                f"{bot}/parsing.py defines {name} again — it lives in "
                "bots_common/parsing.py")
