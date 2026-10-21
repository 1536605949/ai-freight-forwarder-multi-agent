"""Load the bundled SOP corpus into a tenant's knowledge base.

Idempotent: documents are keyed by a checksum of their normalised text, so running this
twice does not duplicate chunks. That matters because a duplicated corpus distorts the
retrieval score distribution and every threshold calibrated against it.

Usage:
    python scripts/seed_knowledge.py                  # tenant-demo
    python scripts/seed_knowledge.py --tenant acme    # another tenant
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.knowledge.service import seed_corpus  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant', default='tenant-demo')
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        results = seed_corpus(db, args.tenant)
    finally:
        db.close()

    created = sum(1 for r in results if r.created)
    print(f'tenant={args.tenant} documents={len(results)} new={created} '
          f'existing={len(results) - created}')
    for row in results:
        flag = 'new ' if row.created else 'keep'
        print(f'  [{flag}] {row.title} ({row.chunk_count} chunks, {row.embedding_model})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
