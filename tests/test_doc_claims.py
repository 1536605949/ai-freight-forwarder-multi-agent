"""The counts the README and CHANGELOG state are claims; these tests make them fail on drift.

This module exists because one of them was already wrong. The v2.0 commit message says the
graph has "12 nodes" while `NODES` has 11, and the CHANGELOG said 10. A number in a document
is a claim about the code, and a claim with nothing behind it is the same defect as a test
that cannot fail -- which is the failure mode this repository keeps trying to eliminate.

Each test reads the published number out of the document and compares it with the code. When
it fails, the fix is either to change the code or to change the document, and the assertion
message says which numbers disagree so the choice is explicit rather than mechanical.

Deliberately not checked: the test count. It cannot be asserted from inside the suite that
counts it without becoming self-referential, and a wrong test count is corrected by every
run, whereas a wrong route count is not.
"""
from __future__ import annotations

import pathlib
import re

from app.agents.tools import TOOL_SPECS
from app.db import Base
from app.graph.builder import NODES

# `import app.models` binds the name `app` to the package and shadows the FastAPI instance,
# so the instance is imported under a name that cannot be shadowed.
import app.models  # noqa: F401,E402  -- registers the tables on Base.metadata
from app.main import app as fastapi_app

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _doc(name: str) -> str:
    return (ROOT / name).read_text(encoding='utf-8')


def _claimed(pattern: str, text: str, *, where: str) -> int:
    match = re.search(pattern, text)
    assert match, f'{where} no longer states a number matching {pattern!r}'
    return int(match.group(1))


def test_readme_route_count_matches_the_app():
    claimed = _claimed(r'\| API 路由 \| \*\*(\d+)\*\*', _doc('README.md'), where='README')
    assert claimed == len(fastapi_app.routes), (
        f'README claims {claimed} routes, the app registers {len(fastapi_app.routes)}. '
        'Update the README table and the main.py line in the structure listing.'
    )


def test_readme_table_and_index_counts_match_the_metadata():
    text = _doc('README.md')
    tables = _claimed(r'\| 数据表 / 索引 \| \*\*(\d+) / (\d+)\*\*', text, where='README')
    indexes = int(re.search(r'\| 数据表 / 索引 \| \*\*\d+ / (\d+)\*\*', text).group(1))

    assert tables == len(Base.metadata.tables), (
        f'README claims {tables} tables, the metadata has {len(Base.metadata.tables)}'
    )
    actual_indexes = sum(len(t.indexes) for t in Base.metadata.tables.values())
    assert indexes == actual_indexes, (
        f'README claims {indexes} indexes, the metadata has {actual_indexes}'
    )


def test_the_node_count_in_the_changelog_matches_the_graph():
    claimed = _claimed(r'把 (\d+) 个节点组织成状态图', _doc('CHANGELOG.md'), where='CHANGELOG')
    assert claimed == len(NODES), (
        f'CHANGELOG claims {claimed} nodes, NODES has {len(NODES)}: {NODES}'
    )


def test_the_readonly_tool_count_matches_the_registry():
    claimed = _claimed(r'当前 (\d+) 个工具全部只读', _doc('README.md'), where='README')
    assert claimed == len(TOOL_SPECS), (
        f'README claims {claimed} tools, the registry has {len(TOOL_SPECS)}'
    )
    # The claim is "all read-only", so assert the substance, not just the count.
    assert all(spec.read_only for spec in TOOL_SPECS)
