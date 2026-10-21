"""Refuse to serve from a schema alembic does not own.

Why the application no longer creates its own tables
----------------------------------------------------
`app/main.py` used to call `Base.metadata.create_all(engine)` on startup outside production.
That looked convenient and was the last remaining second source of truth for the schema,
after the bootstrap and the migrations were both moved onto alembic. It fails in a way that
is hard to connect to its cause:

* `create_all` only *creates missing tables*. It never adds a column to an existing table.
  So the moment a migration adds a column, every developer who has run the app before keeps
  a database that is missing it, and the app starts returning 500 with
  `no such column: agent_runs.prompt_tokens` -- a symptom that points at the query, not at
  the schema.
* The database it builds has no `alembic_version` row, so `alembic upgrade head` then dies
  with `table agent_runs already exists`. The obvious repair (upgrade) is blocked by the
  thing that was supposed to help.

Both of those happened in this repository during the v2.0 work. The fix is to stop creating
anything at startup and to fail loudly instead, because a refused boot with an instruction
is a better failure than a running server with a stale schema.

What the check does
-------------------
Compares the revision recorded in `alembic_version` with the head of the migration scripts.
Anything else -- no table, no row, an older revision -- raises `SchemaNotReady` with the
command that fixes it. Deliberately does **not** run the upgrade: schema changes during a
process start are a production incident waiting to happen, and on a rolling deploy two
instances would race to run the same DDL.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]


class SchemaNotReady(RuntimeError):
    """The database cannot be served: alembic does not have it at head."""

    def __init__(self, reason: str, hint: str):
        super().__init__(f'{reason}\n  fix: {hint}')
        self.reason = reason
        self.hint = hint


def head_revision() -> str | None:
    """The head revision of the migration scripts on disk."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(ROOT / 'migrations'))
    return ScriptDirectory.from_config(cfg).get_current_head()


def current_revision(engine: Engine) -> str | None:
    """The revision recorded in the database, or None if alembic has recorded nothing."""
    inspector = inspect(engine)
    if 'alembic_version' not in inspector.get_table_names():
        return None
    with engine.connect() as conn:
        row = conn.execute(text('SELECT version_num FROM alembic_version')).first()
    return row[0] if row else None


def ensure_schema(engine: Engine) -> str:
    """Return the current revision, or raise `SchemaNotReady` with an actionable message."""
    head = head_revision()
    if head is None:  # pragma: no cover - only if migrations/ is emptied
        raise SchemaNotReady('no migration scripts found', 'restore migrations/versions/')

    current = current_revision(engine)
    if current is None:
        if 'agent_runs' in inspect(engine).get_table_names():
            # The specific state `create_all` produced: tables exist, alembic has no record.
            raise SchemaNotReady(
                'this database has tables but no alembic_version row, so alembic cannot '
                'adopt it (it was built by create_all, not by a migration)',
                'rebuild it with `python scripts/bootstrap_dev.py --reset-db` if it holds '
                'nothing you need, or confirm the schema is current and run '
                '`alembic stamp head`',
            )
        raise SchemaNotReady(
            'the database has no schema',
            'run `python scripts/bootstrap_dev.py`, or `alembic upgrade head`',
        )

    if current != head:
        raise SchemaNotReady(
            f'the database is at revision {current} but the code expects {head}',
            'run `alembic upgrade head` before starting the service',
        )
    return current
