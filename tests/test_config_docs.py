"""`.env.example` is part of the contract, so it is checked rather than trusted.

The file was previously absent entirely while the README told operators to copy it, and the
docs referenced settings (`DEFAULT_MARGIN_PCT`, `RFQ_TIMEOUT_HOURS`) that appeared nowhere.
These tests make both failures impossible:

* every `Settings` field must be documented,
* nothing may be documented that is not a field,
* and the example must actually parse into `Settings` without a validation error.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / '.env.example'

_KEY = re.compile(r'^([A-Z][A-Z0-9_]*)\s*=')


def _documented_keys() -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = _KEY.match(line)
        if m:
            out[m.group(1)] = line.split('=', 1)[1]
    return out


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file(), 'README tells operators to copy .env.example'


def test_every_setting_is_documented():
    fields = {name.upper() for name in Settings.model_fields}
    missing = sorted(fields - set(_documented_keys()))
    assert not missing, f'undocumented settings: {missing}'


def test_no_stale_keys_are_documented():
    fields = {name.upper() for name in Settings.model_fields}
    stale = sorted(set(_documented_keys()) - fields)
    assert not stale, f'.env.example documents settings that do not exist: {stale}'


def test_example_parses_into_settings(tmp_path):
    """A typo'd key or an invalid value must fail here, not in an operator's terminal."""
    env = tmp_path / '.env'
    env.write_text(ENV_EXAMPLE.read_text(encoding='utf-8'), encoding='utf-8')
    # Constructing it is the assertion: pydantic validates types on load.
    s = Settings(_env_file=env)
    assert s.app_name
    assert s.agent_provider in {'mock', 'maf_openai'}
    assert 0 <= s.default_margin_pct < 1
    assert s.minimum_margin_usd > 0


def test_documented_booleans_are_parseable():
    """`true`/`false` in the example must be real booleans, not the string 'true'."""
    env_file = ROOT / '.env.example'
    text = env_file.read_text(encoding='utf-8')
    for key in ('OUTREACH_REQUIRES_HUMAN_APPROVAL',):
        assert re.search(rf'^{key}=(true|false)$', text, re.M), (
            f'{key} must be documented as a bare true/false')
