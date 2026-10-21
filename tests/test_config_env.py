"""Settings read from the environment: REGISTRATION_OPEN and AI_TAGS.

Config reads the environment once, when app.config is imported, so each
case imports it in a fresh interpreter with its own environment. The
/register tests below flip the attribute directly, the way a deployment
flips the variable.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Config

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AI_TAGS = ['fake', 'ai', 'grok', 'deepfake', 'gemini', 'chatgpt']


def _config(**env):
    """REGISTRATION_OPEN and AI_TAGS as a fresh import sees them."""
    environ = {k: v for k, v in os.environ.items()
               if k not in ('REGISTRATION_OPEN', 'AI_TAGS')}
    environ.update(env)
    out = subprocess.run(
        [sys.executable, '-c',
         'import json; from app.config import Config; '
         'print(json.dumps([Config.REGISTRATION_OPEN, list(Config.AI_TAGS)]))'],
        cwd=ROOT, env=environ, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_defaults_keep_registration_closed_and_the_ai_list():
    assert _config() == [False, DEFAULT_AI_TAGS]


@pytest.mark.parametrize('value, expected', [
    ('true', True), ('TRUE ', True), ('false', False),
    ('yes', False), ('1', False), ('', False),
])
def test_registration_opens_only_for_true(value, expected):
    assert _config(REGISTRATION_OPEN=value)[0] is expected


def test_ai_tags_are_a_comma_separated_list():
    assert _config(AI_TAGS=' fake, synthetic ,,ai ')[1] == ['fake', 'synthetic', 'ai']


@pytest.mark.parametrize('value', ['', '  ', ',,'])
def test_an_empty_ai_tags_keeps_the_default_list(value):
    assert _config(AI_TAGS=value)[1] == DEFAULT_AI_TAGS


def test_closed_registration_sends_visitors_to_login(client):
    resp = client.get('/register')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']


def test_open_registration_creates_the_first_account_as_admin(client, monkeypatch):
    from app.auth.db import fetch_user_by_id, fetch_user_for_login
    monkeypatch.setattr(Config, 'REGISTRATION_OPEN', True)
    resp = client.post('/register', data={'username': 'first',
                                          'password': 'password123'})
    assert resp.status_code == 302
    uid = fetch_user_for_login('first')[0]
    assert fetch_user_by_id(uid)[2] is True
