"""MANIFEST.txt / SHA256SUMS.txt must describe exactly what git ships.

These two files are the repository's integrity artifact: ``SHA256SUMS.txt`` is what a
reviewer runs ``sha256sum -c`` against after cloning.  That only means something if it
is regenerated whenever content changes -- and if the hashes are computed over the
bytes a clone actually receives rather than over the local working copy.

The second point is not hypothetical.  The first version of these files was generated
with a plain ``sha256sum`` on a Windows checkout, which hashes CRLF while the repository
stores LF; verification failed for 11 files on a fresh clone.  The
``test_hashes_match_the_committed_blobs`` case below is the regression guard for that.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'MANIFEST.txt'
SUMS = ROOT / 'SHA256SUMS.txt'


def _git(*args: str) -> str:
    return subprocess.run(
        ['git', *args], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture(scope='module')
def tracked() -> list[str]:
    try:
        out = _git('ls-files', '-z')
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip('git is not available')
    return sorted(p for p in out.split('\0') if p)


def _read_entries(path: pathlib.Path) -> list[str]:
    return [
        line for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.startswith('#')
    ]


def _normalised_sha256(relpath: str) -> str:
    raw = (ROOT / relpath).read_bytes()
    if b'\x00' not in raw:
        raw = raw.replace(b'\r\n', b'\n')
    return hashlib.sha256(raw).hexdigest()


def test_manifest_lists_every_tracked_file(tracked):
    listed = sorted(e.removeprefix('./') for e in _read_entries(MANIFEST))
    assert listed == tracked


def test_manifest_has_no_duplicates():
    entries = [e.removeprefix('./') for e in _read_entries(MANIFEST)]
    assert len(entries) == len(set(entries))


def test_checksums_cover_every_tracked_file_except_itself(tracked):
    listed = sorted(e.split('  ', 1)[1].removeprefix('./') for e in _read_entries(SUMS))
    assert listed == [f for f in tracked if f != 'SHA256SUMS.txt']


def test_recorded_hashes_match_the_normalised_content(tracked):
    """A stale hash is a broken integrity guarantee, so this is a hard failure."""
    mismatched = []
    for entry in _read_entries(SUMS):
        recorded, relpath = entry.split('  ', 1)
        relpath = relpath.removeprefix('./')
        if recorded != _normalised_sha256(relpath):
            mismatched.append(relpath)
    assert mismatched == [], f'stale checksums for: {mismatched}'


def test_hashes_match_the_committed_blobs(tracked):
    """Cross-check the normalisation against git's own, on files unchanged since HEAD.

    ``git cat-file blob HEAD:<path>`` yields exactly the bytes a clone writes to disk,
    so it is the ground truth for "what a fresh clone hashes".  Hashing the working
    copy instead is what produced the original CRLF bug.

    Files with pending edits are skipped: their recorded hash legitimately describes
    content that is not in HEAD yet, and the generator/regenerate step is what keeps
    them in step.  ``test_recorded_hashes_match_the_normalised_content`` still covers
    those.  In CI (clean checkout) nothing is skipped.
    """
    try:
        _git('rev-parse', '--verify', 'HEAD')
    except subprocess.CalledProcessError:  # pragma: no cover
        pytest.skip('no commit yet')

    # Content-based, not `git status --porcelain`: the latter can flag a file as
    # modified purely from a stale stat cache (rewriting a file with identical bytes
    # after normalising line endings does exactly that), which would skip entries that
    # are in fact perfectly checkable.
    dirty = set(_git('diff', '--name-only', 'HEAD').splitlines())

    recorded = {
        e.split('  ', 1)[1].removeprefix('./'): e.split('  ', 1)[0]
        for e in _read_entries(SUMS)
    }
    checked, drift = 0, []
    for relpath, want in recorded.items():
        if relpath in dirty:
            continue
        try:
            blob = subprocess.run(
                ['git', 'cat-file', 'blob', f'HEAD:{relpath}'],
                cwd=ROOT, capture_output=True, check=True,
            ).stdout
        except subprocess.CalledProcessError:
            continue                      # untracked-in-HEAD; covered by the other tests
        checked += 1
        if hashlib.sha256(blob).hexdigest() != want:
            drift.append(relpath)

    assert drift == [], (
        f'{len(drift)} recorded hash(es) do not match the committed blob, '
        f'so `sha256sum -c` would fail on a fresh clone: {drift[:5]}'
    )
    # Guard against this test quietly becoming vacuous. Proportional rather than a fixed
    # allowance, so it keeps meaning something as the repository grows.
    checked_share = checked / len(recorded)
    assert checked_share >= 0.9, (
        f'only {checked}/{len(recorded)} entries ({checked_share:.0%}) could be '
        'cross-checked; too much of the tree is dirty for this test to mean anything'
    )


def test_generator_reproduces_the_files_on_disk():
    """The generator is the source of truth; the checked-in files must agree with it."""
    from scripts.gen_checksums import build

    manifest, sums = build()
    assert MANIFEST.read_text(encoding='utf-8') == manifest, (
        'MANIFEST.txt is stale -- run: python scripts/gen_checksums.py'
    )
    assert SUMS.read_text(encoding='utf-8') == sums, (
        'SHA256SUMS.txt is stale -- run: python scripts/gen_checksums.py'
    )
