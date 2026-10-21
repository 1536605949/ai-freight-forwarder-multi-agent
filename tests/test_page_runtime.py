"""Runtime check: boot the demo pages in jsdom and drive their own JavaScript.

`test_demo_pages.py` is static analysis. It can prove a page mentions `hitl_review` and
branches on `answered`; it cannot prove the page renders. This repository has already paid
for that distinction once -- 259 green tests coexisted with a 500 on the first real request.

So this module executes the page: jsdom parses the HTML, runs the inline script, the script's
`fetch` is stubbed with recorded API payloads, and the resulting DOM is inspected after real
button clicks. `scripts/page_smoke.js` holds the fixtures and the assertions; this module only
decides whether the toolchain exists.

The toolchain is optional on purpose. CI installs Python only, so the check runs where
node + jsdom are available and **skips with a reason** where they are not -- a skip that says
why is honest; a green tick that never executed anything is not.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / 'scripts' / 'page_smoke.js'

# jsdom is installed in the managed node workspace on this machine. Other environments may
# put it anywhere; NODE_PATH is honoured first so a project-local install also works.
_JSDOM_CANDIDATES = [
    os.environ.get('NODE_PATH', ''),
    str(Path.home() / '.workbuddy-ai' / 'binaries' / 'node' / 'workspace' / 'node_modules'),
    str(ROOT / 'node_modules'),
]


def _node() -> str | None:
    """A node binary that can resolve jsdom, or None."""
    candidates = [shutil.which('node'), shutil.which('nodejs')]
    managed = Path.home() / '.workbuddy-ai' / 'binaries' / 'node'
    if managed.is_dir():
        candidates += [str(p) for p in sorted(managed.glob('versions/*/node.exe'), reverse=True)]
        candidates += [str(p) for p in sorted(managed.glob('versions/*/bin/node'), reverse=True)]

    for exe in [c for c in candidates if c and Path(c).exists()]:
        for node_path in [p for p in _JSDOM_CANDIDATES if p and Path(p).is_dir()]:
            probe = subprocess.run(
                [exe, '-e', "require.resolve('jsdom')"],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, 'NODE_PATH': node_path},
            )
            if probe.returncode == 0:
                return f'{exe}\x00{node_path}'
    return None


_NODE = _node()


def _run_all() -> dict:
    """Both pages, one node process.

    Requiring jsdom costs ~10s on a cold Windows cache. Paying it once instead of once per
    page is the difference between a 15s check and a 25s check, and this one runs on every
    commit. `page_smoke.js` takes page names as argv, so passing none means "all of them".
    """
    exe, node_path = _NODE.split('\x00')
    proc = subprocess.run(
        [exe, str(SMOKE)],
        capture_output=True, text=True, timeout=300,
        cwd=str(ROOT), env={**os.environ, 'NODE_PATH': node_path},
    )
    assert proc.stdout.strip(), f'page_smoke.js produced no output\nstderr: {proc.stderr[-800:]}'
    return json.loads(proc.stdout)


@pytest.mark.skipif(_NODE is None, reason='node + jsdom not installed; page runtime check is optional')
def test_page_javascript_actually_runs():
    result = _run_all()

    failures = [
        f'  - [{entry["page"]}] {c["name"]} (got {c["detail"]!r})'
        for entry in result['results'] for c in entry['checks'] if not c['pass']
    ]
    assert result.get('ok'), 'failing checks:\n' + '\n'.join(failures)

    # Every page must have been driven, and driven *for real*: a page whose script threw on
    # load would still report `ok` with an empty check list, so count the checks per page.
    assert [e['page'] for e in result['results']] == ['graph', 'knowledge'], (
        f'smoke covered {[e["page"] for e in result["results"]]}, expected both pages'
    )
    for entry in result['results']:
        assert len(entry['checks']) >= 12, (
            f'only {len(entry["checks"])} checks ran for {entry["page"]}; '
            'the smoke script may be broken'
        )


# --- Parse check for pages nobody drives -----------------------------------------------
# `page_smoke.js` drives graph and knowledge. The other three pages have contract tests but
# nothing that would notice a syntax error in their inline script -- and this module's own
# history says a page that never executes is a page that is never verified. `node --check`
# is the cheapest honest middle ground: it proves the script parses for every page, without
# pretending to prove it renders.

def _inline_script(html: str) -> str:
    return '\n'.join(re.findall(r'<script>(.*?)</script>', html, re.S))


_DEMO_PAGES = sorted(p.name for p in (ROOT / 'demo_client').glob('*.html'))


@pytest.mark.skipif(_NODE is None, reason='node not installed; page parse check is optional')
@pytest.mark.parametrize('page', _DEMO_PAGES)
def test_every_demo_page_inline_script_parses(page, tmp_path):
    code = _inline_script((ROOT / 'demo_client' / page).read_text(encoding='utf-8'))
    if not code.strip():
        pytest.skip(f'{page} has no inline script')

    exe, _ = _NODE.split('\x00')
    target = tmp_path / 'page.js'
    target.write_text(code, encoding='utf-8')
    proc = subprocess.run([exe, '--check', str(target)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'{page}: inline script does not parse\n{proc.stderr[-1200:]}'
