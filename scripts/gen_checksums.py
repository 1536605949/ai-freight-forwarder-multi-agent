"""Regenerate MANIFEST.txt and SHA256SUMS.txt from the tracked file list.

Why this exists
---------------
An earlier version of these files was generated with a plain ``sha256sum`` over the
working-copy bytes.  On a Windows checkout that hashes the **CRLF** form, while
``.gitattributes`` (``* text=auto eol=lf``) stores the **LF** form in the repository.
The result: ``sha256sum -c SHA256SUMS.txt`` failed for every text file that happened
to be checked out with CRLF -- i.e. the integrity artifact did not validate on the
very clone it was meant to protect.

This script hashes what a clone actually receives:

* binary files (a NUL byte is present, the same heuristic git uses for ``text=auto``)
  are hashed verbatim;
* text files are normalised to LF before hashing.

Both files are written with LF endings so the generator is idempotent across
platforms.

Usage::

    python scripts/gen_checksums.py            # write the files
    python scripts/gen_checksums.py --check    # exit 1 if they are stale (CI)
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

MANIFEST_HEADER = (
    '# Generated file list -- what ships in this repository.\n'
    '# Regenerate with:  python scripts/gen_checksums.py\n'
)
SUMS_HEADER = (
    '# SHA-256 of every tracked file except this one.\n'
    '# Verify with:  sha256sum -c SHA256SUMS.txt\n'
    '# Regenerate with:  python scripts/gen_checksums.py\n'
)


def tracked_files() -> list[str]:
    """Every path git tracks, POSIX-separated, sorted."""
    out = subprocess.run(
        ['git', 'ls-files', '-z'],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return sorted(p for p in out.split('\0') if p)


def content_sha256(relpath: str) -> str:
    """SHA-256 of the bytes a fresh clone would materialise for ``relpath``."""
    raw = (ROOT / relpath).read_bytes()
    if b'\x00' not in raw:          # text file -> git normalises CRLF to LF
        raw = raw.replace(b'\r\n', b'\n')
    return hashlib.sha256(raw).hexdigest()


def build() -> tuple[str, str]:
    files = tracked_files()
    manifest = MANIFEST_HEADER + '\n'.join('./' + f for f in files) + '\n'
    # MANIFEST.txt cannot be hashed from disk: it is rewritten by this very run, so
    # reading it back would record the *previous* content.  Hash the new content.
    manifest_digest = hashlib.sha256(manifest.encode('utf-8')).hexdigest()
    rows = [
        f'{manifest_digest if f == "MANIFEST.txt" else content_sha256(f)}  ./{f}'
        for f in files
        if f != 'SHA256SUMS.txt'
    ]
    sums = SUMS_HEADER + '\n'.join(rows) + '\n'
    return manifest, sums


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        '--check', action='store_true',
        help='do not write; exit 1 if the files on disk are stale',
    )
    args = ap.parse_args(argv)

    manifest, sums = build()
    targets = {'MANIFEST.txt': manifest, 'SHA256SUMS.txt': sums}

    stale = []
    for name, want in targets.items():
        path = ROOT / name
        have = path.read_text(encoding='utf-8') if path.exists() else None
        if have != want:
            stale.append(name)

    if args.check:
        if stale:
            print('STALE: ' + ', '.join(stale))
            print('Run: python scripts/gen_checksums.py')
            return 1
        print('CHECKSUM MANIFEST OK -- up to date')
        return 0

    for name, want in targets.items():
        (ROOT / name).write_text(want, encoding='utf-8', newline='\n')
        print(f'wrote {name}')
    print(f'{len(tracked_files())} tracked files, '
          f'{sums.count(chr(10)) - SUMS_HEADER.count(chr(10))} checksums')
    return 0


if __name__ == '__main__':
    sys.exit(main())
