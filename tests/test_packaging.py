"""Packaging invariants.

These exist because a dependency conflict once broke `pip install -r requirements.txt`
(and therefore the Docker build) for an unknown length of time without anyone noticing.
A broken install is a P0 that no application test would catch, so it gets its own guard.
"""
import pathlib
import re
import pytest

ROOT=pathlib.Path(__file__).resolve().parents[1]


def _pins(path):
    """Return {normalized_name: specifier} for non-comment lines in a requirements file."""
    out={}
    for raw in (ROOT/path).read_text(encoding='utf-8').splitlines():
        line=raw.split('#')[0].strip()
        if not line or line.startswith('-r '): continue
        m=re.match(r'^([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(.*)$',line)
        if m: out[m.group(1).lower().replace('_','-')]=m.group(2).strip()
    return out


def test_base_requirements_exclude_optional_agent_framework():
    """agent-framework must not be in the base install: it conflicts with the fastapi pin."""
    assert 'agent-framework' not in _pins('requirements.txt'), (
        'agent-framework belongs in requirements-agent.txt, not requirements.txt — '
        'pinning it here breaks `pip install -r requirements.txt` and the Docker build.'
    )


def test_agent_framework_available_opt_in():
    assert 'agent-framework' in _pins('requirements-agent.txt')


def test_dev_requirements_chain_to_base():
    text=(ROOT/'requirements-dev.txt').read_text(encoding='utf-8')
    assert '-r requirements.txt' in text


def test_dockerfile_does_not_install_optional_agent_package():
    """The image must build from the base requirements, which are conflict-free."""
    docker=(ROOT/'Dockerfile').read_text(encoding='utf-8')
    assert 'requirements-agent.txt' not in docker, (
        'Dockerfile must not install the optional agent framework; it would fail to resolve.'
    )


@pytest.mark.parametrize('path',['requirements.txt','requirements-dev.txt','requirements-agent.txt'])
def test_requirements_files_are_parseable(path):
    assert (ROOT/path).exists()
    assert _pins(path) or '-r ' in (ROOT/path).read_text(encoding='utf-8')
