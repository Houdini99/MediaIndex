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

import bots_common.download as common_download
import bots_common.index_client as common_index
import bots_common.parsing as common_parsing
from discord_bot import config as discord_config
from discord_bot import download as discord_download
from discord_bot import index_client as discord_index
from discord_bot import parsing as discord_parsing
from telegram_bot import config as telegram_config
from telegram_bot import download as telegram_download
from telegram_bot import index_client as telegram_index
from telegram_bot import parsing as telegram_parsing

REPO = pathlib.Path(__file__).resolve().parent.parent

SHARED = ('extract_urls', 'strip_urls', 'media_url_info',
          'parse_message_tags', 'format_progress')


@pytest.mark.parametrize('name', SHARED)
def test_both_bots_parse_with_the_shared_code(name):
    shared = getattr(common_parsing, name)
    assert getattr(discord_parsing, name) is shared
    assert getattr(telegram_parsing, name) is shared


def test_both_bots_download_with_the_shared_fetch():
    """The address check and the cap live in one place, so neither bot can
    fetch a link without them."""
    assert discord_download.fetch is common_download.fetch
    assert telegram_download.fetch is common_download.fetch


@pytest.mark.parametrize('downloader', [discord_download.CdnDownloader,
                                        telegram_download.LinkDownloader])
def test_both_bots_keep_their_session_in_the_shared_downloader(downloader):
    assert issubclass(downloader, common_download.Downloader)
    assert not {'session', 'close', 'sniff_links'} & set(vars(downloader))


def test_redirects_are_followed_in_one_place():
    """Every hop has to pass require_public. A second hand-written redirect
    loop is a second place to forget it: use sniff.follow_public."""
    loops = [str(path.relative_to(REPO))
             for pkg in ('bots_common', 'discord_bot', 'telegram_bot')
             for path in sorted((REPO / pkg).glob('*.py'))
             if 'allow_redirects=False' in path.read_text()]
    assert loops == ['bots_common/sniff.py']


@pytest.mark.parametrize('client', [discord_index.IndexClient,
                                    telegram_index.IndexClient])
def test_both_bots_talk_to_the_index_through_the_shared_client(client):
    """Each bot adds its own identity lookup, and nothing else: the
    upload, the cooldown and the lookup cache exist once."""
    assert issubclass(client, common_index.IndexClient)
    own = {name for name in vars(client) if not name.startswith('__')}
    assert own <= {'lookup_discord_user', 'verify_discord_link',
                   'lookup_telegram_user'}, own


@pytest.mark.parametrize('config', [discord_config, telegram_config])
def test_both_bots_accept_the_shared_file_types(config):
    assert config.IMAGE_EXT is common_parsing.IMAGE_EXT
    assert config.ALLOWED_EXT is common_parsing.ALLOWED_EXT


def test_no_private_copies_came_back():
    for bot in ('discord_bot', 'telegram_bot'):
        assert not (REPO / bot / 'resolver.py').exists(), (
            f"{bot}/resolver.py is back — import bots_common.resolver instead")
        for name in SHARED:
            src = (REPO / bot / 'parsing.py').read_text()
            assert f'\ndef {name}(' not in src, (
                f"{bot}/parsing.py defines {name} again — it lives in "
                "bots_common/parsing.py")
        src = (REPO / bot / 'config.py').read_text()
        for name in ('_require', '_num'):
            assert f'\ndef {name}(' not in src, (
                f"{bot}/config.py defines {name} again — use "
                "bots_common/env.py")
        assert ".lower() != 'false'" not in src, (
            f"{bot}/config.py reads a switch by hand — use env.flag()")
