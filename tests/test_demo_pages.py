"""Contract tests for the v2.0 demo pages (graph runner, knowledge console, landing).

`test_demo_contract.py` pins overview.html to `orchestrator.STAGES`. The pages added with
v2.0 need the same treatment for a different reason: they expose *new* capabilities, and the
failure mode is identical -- a renamed node or a renamed JSON field does not raise, it
renders a blank panel that looks like a slow backend.

Three properties are asserted beyond the shared structural checks:

* the graph page renders exactly the nodes the graph has, in order;
* the graph page offers no way to approve a quote without a human clicking (the whole
  point of the page is that the gate is real);
* the knowledge page treats `answered=false` as a result, not an error -- a page that
  renders a refusal as a red "failed" box would teach the operator the wrong lesson.

No browser is involved: static analysis plus assertions against the real backend constants.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.graph.builder import NODES

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO = ROOT / 'demo_client'

GRAPH = DEMO / 'graph.html'
KNOWLEDGE = DEMO / 'knowledge.html'
INDEX = DEMO / 'index.html'
THEME = DEMO / 'theme.css'
ALL_PAGES = (INDEX, DEMO / 'overview.html', GRAPH, KNOWLEDGE, DEMO / 'prospecting.html')
V2_PAGES = (GRAPH, KNOWLEDGE)


def _html(path: pathlib.Path) -> str:
    assert path.exists(), f'{path.name} is part of the delivery'
    return path.read_text(encoding='utf-8')


def _script(html: str) -> str:
    m = re.search(r'<script>([\s\S]*?)</script>', html)
    assert m, 'page must contain an inline script'
    return m.group(1)


# --------------------------------------------------------------------------- #
# The graph page is bound to the real graph
# --------------------------------------------------------------------------- #

def test_graph_page_renders_every_node_of_the_real_graph():
    js = _script(_html(GRAPH))
    block = re.search(r'const NODE_META=\{(.*?)\n\};', js, re.S)
    assert block, 'NODE_META must stay a literal object so it can be checked'
    keys = re.findall(r'^\s{2}(\w+):\s*\{', block.group(1), re.M)

    assert keys == list(NODES), (
        'graph.html NODE_META and app/graph/builder.NODES have drifted apart.\n'
        f'  page:  {keys}\n  graph: {list(NODES)}'
    )


def test_graph_page_marks_the_gated_nodes():
    js = _script(_html(GRAPH))
    for gate in ('hitl_review', 'quote_send'):
        assert re.search(rf'^\s{{2}}{gate}:\s*\{{[^}}]*gate:true', js, re.M), (
            f'{gate} must be marked gate:true in graph.html'
        )


def test_graph_page_cannot_approve_without_a_human():
    """No auto-approve path in the page, and no synthetic quotes by default.

    The overview page has a labelled "一键直通" button because the legacy orchestrator has
    an auto_approve flag. The graph deliberately has no such flag, so the page must not
    invent one -- and the demo-only quote seeding must be an unchecked checkbox.
    """
    html = _html(GRAPH)
    js = _script(html)

    assert 'auto_approve' not in js, 'the graph has no auto-approve path; the page must not offer one'
    assert 'id="synth"' in html, 'synthetic quote seeding must be an explicit control'
    assert not re.search(r'id="synth"[^>]*checked', html), (
        'synthetic quotes must default to off; they are demo data, not a shortcut'
    )
    # the flag is sent explicitly, driven by the checkbox, never hardcoded true
    calls = re.findall(r'allow_synthetic_quotes:\s*([^,}\n]+)', js)
    assert calls, 'the page must send allow_synthetic_quotes explicitly'
    assert all(c.strip() == "$('synth').checked" for c in calls), calls


def test_graph_page_calls_the_endpoints_it_claims():
    html = _html(GRAPH)
    for endpoint in ('/api/v1/graph/run', '/api/v1/graph/resume', '/api/v1/graph/status/',
                     '/api/v1/inquiries', '/api/v1/rfqs/', '/supplier-quotes',
                     '/api/v1/quotes/'):
        assert endpoint in html, f'graph.html must call {endpoint}'


# --------------------------------------------------------------------------- #
# The knowledge page must not present a refusal as a failure
# --------------------------------------------------------------------------- #

def test_knowledge_page_branches_on_answered_and_names_refusal_as_correct():
    js = _script(_html(KNOWLEDGE))

    assert 'j.answered' in js, 'the page must branch on answered'
    assert '这是正确结果' in js, (
        'a refusal must be described as a correct outcome, not as an error'
    )
    # A refusal is not thrown: the only throws are for transport/HTTP failures.
    refusal_block = js.split('if(!j.answered){', 1)[1].split('return;', 1)[0]
    assert 'throw' not in refusal_block, 'answered=false is a result, not an exception'


def test_knowledge_page_calls_the_endpoints_it_claims():
    html = _html(KNOWLEDGE)
    for endpoint in ('/api/v1/knowledge/answer', '/api/v1/knowledge/search',
                     '/api/v1/knowledge/documents', '/api/v1/knowledge/seed',
                     '/api/v1/agent/ask', '/api/v1/metrics/agent'):
        assert endpoint in html, f'knowledge.html must call {endpoint}'


def test_knowledge_page_explains_the_null_cost_instead_of_printing_zero():
    """Cost per run is null when nothing reported usage; the page must say so."""
    js = _script(_html(KNOWLEDGE))
    assert 'per_reported_run' in js
    assert '不是 0' in js or '不是 <b>0</b>' in js, (
        'the page must distinguish "unknown cost" from "zero cost"'
    )


def test_knowledge_page_offers_an_out_of_domain_sample():
    """The refusal demo is only reachable if a question the corpus cannot answer is offered."""
    html = _html(KNOWLEDGE)
    assert '公司食堂' in html, 'the sample set must include a question the corpus cannot answer'
    assert '域外' in html


# --------------------------------------------------------------------------- #
# Shared structural checks for the new pages
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('path', V2_PAGES)
def test_v2_page_scripts_are_balanced(path):
    js = _script(_html(path))
    stripped = re.sub(r"'[^'\n]*'", "''", js)
    stripped = re.sub(r'`[^`]*`', '``', stripped, flags=re.S)
    for open_c, close_c in (('{', '}'), ('(', ')'), ('[', ']')):
        assert stripped.count(open_c) == stripped.count(close_c), (
            f'{path.name}: unbalanced {open_c}{close_c}'
        )


@pytest.mark.parametrize('path', V2_PAGES)
def test_v2_page_dom_references_resolve(path):
    """Every `$('x')` must have something that creates `id="x"`.

    Unlike the overview check, ids are collected from the whole file, not just the static
    markup: the gate panels are rendered from templates, so `fixEtd` and the supplier-quote
    inputs are created by the page itself. The property being asserted is "this reference
    can resolve", not "this element exists before any script runs" -- a typo like
    `$('sqNamee')` still fails, which is the failure this test is for.
    """
    html = _html(path)
    js = _script(html)
    ids = set(re.findall(r'id="([^"]+)"', html))
    refs = set(re.findall(r"\$\('([^']+)'\)", js)) | set(
        re.findall(r"getElementById\('([^']+)'\)", js))
    missing = {r for r in refs - ids if '${' not in r}
    assert not missing, f'{path.name} references ids nothing creates: {sorted(missing)}'


@pytest.mark.parametrize('path', V2_PAGES)
def test_v2_page_escapes_dynamic_text(path):
    js = _script(_html(path))
    assert 'function esc(' in js, f'{path.name} interpolates server data without an escape helper'


@pytest.mark.parametrize('path', V2_PAGES)
def test_v2_page_uses_relative_urls(path):
    html = _html(path)
    assert 'http://localhost' not in html and '127.0.0.1' not in html, (
        f'{path.name} hardcodes a host; use relative URLs'
    )


@pytest.mark.parametrize('path', V2_PAGES)
def test_v2_page_links_the_shared_stylesheet(path):
    html = _html(path)
    assert '<link rel="stylesheet" href="theme.css">' in html, (
        f'{path.name} must use the shared shell so the suite reads as one product'
    )
    assert THEME.exists()


def test_theme_palette_matches_the_pages_that_keep_their_own_copy():
    """theme.css must not become a second visual language.

    overview.html predates the shared sheet and keeps its own tokens; the two must agree on
    the values a user actually sees, or the suite will look like two products.
    """
    theme = _html(THEME)
    overview = _html(DEMO / 'overview.html')
    for token in ('--bg', '--ink', '--amber', '--teal', '--red', '--line'):
        pattern = rf'{token}:\s*([^;]+);'
        a = re.search(pattern, theme)
        b = re.search(pattern, overview)
        assert a and b, f'{token} missing from one of the sheets'
        assert a.group(1).strip() == b.group(1).strip(), (
            f'{token} differs: theme={a.group(1).strip()!r} overview={b.group(1).strip()!r}'
        )


# --------------------------------------------------------------------------- #
# Navigation: five pages, one product
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('path', ALL_PAGES)
def test_every_demo_page_links_every_other_page(path):
    html = _html(path)
    for other in ('index.html', 'overview.html', 'graph.html', 'knowledge.html',
                  'prospecting.html'):
        assert f'href="{other}"' in html, f'{path.name} does not link {other}'
    assert 'class="topnav"' in html


def test_landing_page_describes_each_entry_and_its_prerequisite():
    """A landing page that only lists names teaches nothing; each card states what it needs."""
    html = _html(INDEX)
    for phrase in ('业务闭环', '图编排', '知识台', '主动获客'):
        assert phrase in html
    assert html.count('class="need"') == 4, 'each entry must state its prerequisite'
    assert 'bootstrap_dev.py' in html, 'the landing page must say how to start from zero'
