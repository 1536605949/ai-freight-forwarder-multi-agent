"""Pins the status of `app/agents/maf_workflows.py`: an unwired reference implementation.

Dead code that looks live is a trap for the next person. Rather than delete the only worked
example of the real-LLM orchestration path, its status is asserted here, so:

* importing the module must never require the optional `agent_framework` dependency,
* calling a factory without that dependency must fail with an actionable message rather than
  a bare `ModuleNotFoundError`,
* and the set of factories is pinned, so one cannot quietly be wired in without a reviewer
  noticing this test change.

See docs/ARCHITECTURE_REVIEW.md (P2-5).
"""
from __future__ import annotations

import ast
import builtins
import importlib
from pathlib import Path

import pytest

MODULE = 'app.agents.maf_workflows'
SOURCE = Path(__file__).resolve().parents[1] / 'app' / 'agents' / 'maf_workflows.py'


def test_module_imports_without_the_optional_dependency():
    """Every framework import is inside a function, so importing is always safe."""
    mod = importlib.import_module(MODULE)
    assert mod is not None


def test_module_declares_itself_unwired():
    """The docstring must say so, or a reader will assume it is live."""
    text = SOURCE.read_text(encoding='utf-8')
    assert 'NOT WIRED' in text
    assert 'reference implementation' in text.lower()


@pytest.mark.parametrize('factory', [
    'build_inquiry_sequential_workflow',
    'build_quote_concurrent_review_workflow',
    'build_sales_handoff_workflow',
])
def test_factory_fails_with_an_actionable_message_when_the_dep_is_missing(factory, monkeypatch):
    mod = importlib.import_module(MODULE)
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == 'agent_framework' or name.startswith('agent_framework.'):
            raise ImportError('No module named agent_framework')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', blocked)
    with pytest.raises(ImportError) as exc:
        getattr(mod, factory)()
    assert 'requirements-agent.txt' in str(exc.value), 'the error must say how to fix it'


def test_factory_list_is_pinned():
    """Adding a factory is a deliberate act; this test makes it visible in review."""
    mod = importlib.import_module(MODULE)
    assert set(mod.WORKFLOW_FACTORIES) == {
        'build_inquiry_sequential_workflow',
        'build_quote_concurrent_review_workflow',
        'build_sales_handoff_workflow',
    }
    for name in mod.WORKFLOW_FACTORIES:
        assert callable(getattr(mod, name))


def test_nothing_in_the_app_imports_these_workflows():
    """The orphan claim is checked, not assumed.

    If this ever fails, the module has been wired up -- at which point it needs real
    integration tests and this file should be replaced, not deleted.
    """
    app_root = Path(__file__).resolve().parents[1] / 'app'
    offenders = []
    for path in app_root.rglob('*.py'):
        if path.name == 'maf_workflows.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and 'maf_workflows' in node.module:
                offenders.append(str(path.relative_to(app_root)))
            elif isinstance(node, ast.Import):
                if any('maf_workflows' in a.name for a in node.names):
                    offenders.append(str(path.relative_to(app_root)))
    assert not offenders, (
        f'{offenders} now import maf_workflows; it is no longer an orphan, so give it real '
        'integration tests and update docs/ARCHITECTURE_REVIEW.md')
