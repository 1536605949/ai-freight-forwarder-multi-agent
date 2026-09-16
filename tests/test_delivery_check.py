"""Delivery-check script integrity.

`scripts/delivery_check.py` is the artefact a bid evaluator runs to convince themselves the
system works. Two properties matter, and both are cheap to break:

* it must be runnable and parse cleanly (a syntax error here looks like a broken product),
* it must exit non-zero on failure (a check that always passes is worse than no check).

The script drives HTTP against a live server, so it is not executed here - only its
structure and guard rails are asserted.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'delivery_check.py'
MAKEFILE = ROOT / 'Makefile'


@pytest.fixture(scope='module')
def source() -> str:
    assert SCRIPT.exists(), 'scripts/delivery_check.py is referenced by the Makefile'
    return SCRIPT.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def tree(source: str) -> ast.Module:
    return ast.parse(source)


def test_script_parses(tree: ast.Module) -> None:
    assert tree.body, 'script must not be empty'


def test_exit_code_is_non_zero_on_failure(source: str) -> None:
    """A check that cannot fail is not a check."""
    assert 'return 1' in source, 'must return 1 when checks fail'
    assert 'return 0' in source, 'must return 0 when all checks pass'
    assert 'sys.exit(main())' in source, 'must propagate the exit code'


def test_no_dead_helper_functions(tree: ast.Module) -> None:
    """Dead code in a delivery script undermines confidence in it."""
    defined = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    dead = [f for f in defined if f not in called and f != 'main']
    assert not dead, f'uncalled functions in delivery_check.py: {dead}'


def test_covers_the_claims_that_matter(source: str) -> None:
    """Each core business promise must have an explicit check."""
    required = {
        '询价接入': 'messages/inbound',
        '幂等': 'idempotent',
        '编排闸门': 'orchestrate/run',
        '闸门不越权': 'orchestrate/continue',
        '报价硬门禁': '/send',
        '订舱': 'booking_id',
        '船位': 'vessel_tracking',
        '运价来源': 'demo_',
        '租户隔离': 'tenant-other',
        '合规门禁': 'compliance_basis_required',
    }
    for label, needle in required.items():
        assert needle in source, f'delivery check must cover {label} (looked for {needle!r})'


def test_reports_a_summary(source: str) -> None:
    assert 'DELIVERY CHECK PASSED' in source
    assert 'DELIVERY CHECK FAILED' in source


def test_base_url_is_configurable(source: str) -> None:
    """Hard-coding localhost:8100 would make Docker/port-forwards untestable."""
    assert '--base-url' in source
    assert "default='http://localhost:8100'" in source


def test_gives_actionable_advice_when_the_server_is_down(source: str) -> None:
    """The most common first-run failure is 'forgot to start the server'."""
    assert 'uvicorn' in source, 'must tell the user how to start the server'


def test_makefile_target_matches_the_script_path(source: str) -> None:
    """`make delivery-check` must invoke a script that actually exists."""
    makefile = MAKEFILE.read_text(encoding='utf-8')
    m = re.search(r'^delivery-check:\n\t(.+)$', makefile, re.M)
    assert m, 'Makefile must define a delivery-check target'
    cmd = m.group(1)
    assert 'scripts/delivery_check.py' in cmd
    assert SCRIPT.exists()


def test_tenant_headers_are_dev_only(source: str) -> None:
    """The script relies on AUTH_MODE=dev header auth; it must say so."""
    assert 'X-Demo-User' in source
    assert 'X-Demo-Tenant' in source
