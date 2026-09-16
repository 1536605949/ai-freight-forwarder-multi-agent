"""Guards for the developer onboarding path.

`scripts/bootstrap_dev.py` is the first thing a new engineer (or a bid evaluator)
runs. If it breaks, the project *looks* broken even when the application is fine.
These tests pin down the properties that make it trustworthy, and — critically —
prove it never mutates the working tree when asked only to check.

The tests here never invoke pip and never create a venv: they exercise argument
parsing, the dry-run path, and the invariants that make the script idempotent.
"""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / 'scripts' / 'bootstrap_dev.py'


def _import_script():
    """Import bootstrap_dev by path so we can unit-test its pure helpers."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('bootstrap_dev', BOOTSTRAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bootstrap_script_exists_and_compiles():
    assert BOOTSTRAP.exists()
    source = BOOTSTRAP.read_text(encoding='utf-8')
    compile(source, str(BOOTSTRAP), 'exec')


def test_default_args_are_safe():
    """With no flags the script must do the *full* bootstrap, not a silent no-op."""
    mod = _import_script()
    args = mod.parse_args([])
    assert args.check is False
    assert args.reset_db is False, 'resetting the DB must never be the default'
    assert args.skip_install is False


def test_reset_db_is_explicitly_opt_in():
    mod = _import_script()
    assert mod.parse_args(['--reset-db']).reset_db is True


def test_help_works_without_dependencies():
    """`--help` must work on a bare interpreter, before anything is installed."""
    result = subprocess.run(
        [sys.executable, str(BOOTSTRAP), '--help'],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    for flag in ('--skip-install', '--check', '--reset-db', '--json'):
        assert flag in result.stdout


def test_check_mode_is_read_only(tmp_path):
    """`--check` must not create or modify anything in the repo."""
    tracked = ['data', '.venv']
    before = {
        name: (ROOT / name).exists()
        for name in tracked
    }
    db_before = None
    db = ROOT / 'data' / 'freight.db'
    if db.exists():
        db_before = db.stat().st_mtime_ns

    result = subprocess.run(
        [sys.executable, str(BOOTSTRAP), '--check', '--quiet'],
        capture_output=True, text=True, cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    after = {name: (ROOT / name).exists() for name in tracked}
    assert after == before, '--check created or removed something in the working tree'
    if db_before is not None:
        assert db.stat().st_mtime_ns == db_before, '--check wrote to the dev database'


def test_json_mode_emits_machine_readable_output():
    """CI needs to parse the result without scraping human text."""
    result = subprocess.run(
        [sys.executable, str(BOOTSTRAP), '--check', '--json'],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert isinstance(payload, dict)
    assert 'ok' in payload


def test_bootstrap_error_carries_an_actionable_hint():
    """Every failure path must tell the operator what to do next."""
    mod = _import_script()
    err = mod.BootstrapError('boom', hint='do the thing')
    assert err.hint == 'do the thing'
    assert mod.BootstrapError('boom').hint is None


def test_venv_python_path_is_platform_appropriate():
    mod = _import_script()
    p = str(mod.venv_python())
    if mod.IS_WINDOWS:
        assert p.endswith(r'Scripts\python.exe') or p.endswith('Scripts/python.exe')
    else:
        assert p.endswith('bin/python')


@pytest.mark.parametrize('name', ['HOW_TO_RUN.txt', 'Makefile'])
def test_quickstart_docs_mention_bootstrap(name):
    """The onboarding script must be discoverable from the docs."""
    text = (ROOT / name).read_text(encoding='utf-8')
    assert 'bootstrap_dev.py' in text or 'bootstrap' in text
