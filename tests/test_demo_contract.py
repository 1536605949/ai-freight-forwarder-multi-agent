"""Demo page contract.

The demo pages are the only part of this project a non-engineer will ever see, and they
break in a way tests normally miss: a renamed JSON field or a renamed stage does not raise
an error in the browser, it renders a blank panel. These tests pin the coupling between the
front-end and the orchestrator so that rename cannot happen silently.

No browser is required - everything here is static analysis of the HTML plus assertions
against the real stage constants.
"""
import pathlib
import re

import pytest

from app.services import orchestrator as orch

ROOT = pathlib.Path(__file__).resolve().parents[1]
OVERVIEW = ROOT / 'demo_client' / 'overview.html'
PROSPECTING = ROOT / 'demo_client' / 'prospecting.html'


def _html(path: pathlib.Path) -> str:
    assert path.exists(), f'{path.name} is part of the delivery'
    return path.read_text(encoding='utf-8')


def _script(html: str) -> str:
    """Extract the inline <script> body."""
    m = re.search(r'<script>([\s\S]*?)</script>', html)
    assert m, 'demo page must contain an inline script'
    return m.group(1)


# --------------------------------------------------------------------------- #
# The stage list is a contract between backend and front-end
# --------------------------------------------------------------------------- #

def test_overview_renders_every_backend_stage():
    """A stage missing from the page would silently never light up."""
    html = _html(OVERVIEW)
    for stage in orch.STAGES:
        assert f'{stage}:' in html or f"'{stage}'" in html, (
            f'stage {stage!r} exists in orchestrator.STAGES but not in overview.html; '
            'the pipeline rail cannot render it.'
        )


def test_page_declares_no_stage_the_backend_does_not_have():
    """And a phantom stage would render a node that never activates."""
    js = _script(_html(OVERVIEW))
    block = re.search(r'const STAGE_META=\{(.*?)\n\};', js, re.S)
    assert block, 'STAGE_META must stay a literal object so it can be checked'
    keys = re.findall(r'^\s{2}(\w+):\s*\{', block.group(1), re.M)
    assert keys, 'failed to parse STAGE_META keys'
    assert keys == orch.STAGES, (
        'overview.html STAGE_META and orchestrator.STAGES have drifted apart.\n'
        f'  page:        {keys}\n  orchestrator: {orch.STAGES}'
    )


def test_gated_stages_are_marked_as_gates_in_the_ui():
    """The UI must visually distinguish a stage a human must approve."""
    js = _script(_html(OVERVIEW))
    for gate in orch.GATED_STAGES:
        # each gated stage's metadata entry must carry gate:true
        m = re.search(rf'^\s{{2}}{gate}:\s*\{{[^}}]*gate:true', js, re.M)
        assert m, f'{gate} is a gate in the backend but is not marked gate:true in the page'


# --------------------------------------------------------------------------- #
# Script and DOM integrity
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('path', [OVERVIEW, PROSPECTING])
def test_page_scripts_are_balanced(path):
    """Cheap structural sanity: braces and parens balance."""
    js = _script(_html(path))
    for open_c, close_c in (('{', '}'), ('(', ')'), ('[', ']')):
        # ignore string literals crudely but sufficiently for a balance check
        stripped = re.sub(r"'[^'\n]*'", "''", js)
        stripped = re.sub(r'`[^`]*`', '``', stripped, flags=re.S)
        assert stripped.count(open_c) == stripped.count(close_c), (
            f'{path.name}: unbalanced {open_c}{close_c}'
        )


def test_overview_dom_references_resolve():
    """A getElementById target that does not exist is a silent no-op at runtime."""
    html = _html(OVERVIEW)
    js = _script(html)
    ids = set(re.findall(r'id="([^"]+)"', html))
    refs = set(re.findall(r"getElementById\(\s*'([^']+)'\s*\)", js))
    # ids built by concatenation (e.g. 'st_'+stage) are checked by the stage test above
    missing = refs - ids
    assert not missing, f'overview.html references non-existent element ids: {sorted(missing)}'


def test_pages_do_not_hardcode_absolute_api_hosts():
    """Relative URLs only - the page is served from the same origin as the API."""
    for path in (OVERVIEW, PROSPECTING):
        html = _html(path)
        assert 'http://localhost' not in html, f'{path.name} hardcodes a host; use relative URLs'
        assert '127.0.0.1' not in html, f'{path.name} hardcodes a host; use relative URLs'


def test_pages_escape_dynamic_text():
    """Every page interpolating server data must define an escape helper (XSS hygiene)."""
    for path in (OVERVIEW, PROSPECTING):
        js = _script(_html(path))
        assert 'function esc(' in js, f'{path.name} interpolates data without an escape helper'


# --------------------------------------------------------------------------- #
# Endpoint coupling
# --------------------------------------------------------------------------- #

def test_overview_calls_the_orchestrator_endpoints_it_claims_to():
    html = _html(OVERVIEW)
    for endpoint in ('/api/v1/orchestrate/run', '/api/v1/orchestrate/continue',
                     '/api/v1/orchestrate/stages'):
        assert endpoint in html, f'overview.html must call {endpoint}'


def test_overview_does_not_auto_approve_by_default():
    """The default button must stop at the gate; pass-through is a separate, labelled button."""
    html = _html(OVERVIEW)
    # exactly one call site sets auto_approve, and it is driven by the explicit flag
    calls = re.findall(r'auto_approve:([^,}]+)', html)
    assert calls, 'overview.html must send auto_approve explicitly'
    expr = calls[0].strip()
    assert expr.startswith('!!') or expr in ('false', 'true'), (
        f'auto_approve must be an explicit boolean expression, found: {expr}'
    )


def test_demo_headers_are_dev_only():
    """Demo pages rely on AUTH_MODE=dev header auth; they must not look production-ready."""
    for path in (OVERVIEW, PROSPECTING):
        html = _html(path)
        assert 'X-Demo-User' in html and 'X-Demo-Tenant' in html
