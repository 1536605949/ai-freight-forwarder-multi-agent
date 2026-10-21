"""The schema guard: refuse to serve from a database alembic does not own.

The behaviour being tested was added after it had already gone wrong. `create_all` on the
app's startup hook produced a database with tables but no `alembic_version` row, and because
`create_all` never adds a column to an existing table, that database silently lacked the
columns a migration had added. The visible symptom was a 500 with
`no such column: agent_runs.prompt_tokens` -- which reads like a query bug, and whose
obvious repair (`alembic upgrade head`) was blocked by `table agent_runs already exists`.

So the properties here are the ones that would have made that failure obvious in one line:

* a database with no schema is refused with the command that builds one;
* a database built by `create_all` is named as such, with both ways out;
* a database behind head names the two revisions;
* the app no longer creates tables on startup -- asserted on the AST, because a comment
  saying so is not a guarantee.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from sqlalchemy import create_engine, text

from app.db import Base
from app.schema_guard import SchemaNotReady, current_revision, ensure_schema, head_revision

import app.models  # noqa: F401,E402  -- registers the tables on Base.metadata

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _engine():
    return create_engine('sqlite://', connect_args={'check_same_thread': False})


def test_an_empty_database_is_refused_with_the_command_that_builds_it():
    engine = _engine()
    try:
        with pytest.raises(SchemaNotReady) as excinfo:
            ensure_schema(engine)
        assert 'no schema' in excinfo.value.reason
        assert 'alembic upgrade head' in excinfo.value.hint
    finally:
        engine.dispose()


def test_a_create_all_database_is_named_as_such_rather_than_told_to_upgrade():
    """The state that actually occurred: tables present, no alembic_version row.

    Telling the operator to run `alembic upgrade head` here would send them into
    `table agent_runs already exists`, so the message has to offer the two real exits.
    """
    engine = _engine()
    try:
        Base.metadata.create_all(engine)
        assert current_revision(engine) is None

        with pytest.raises(SchemaNotReady) as excinfo:
            ensure_schema(engine)

        assert 'create_all' in excinfo.value.reason
        assert 'stamp head' in excinfo.value.hint
        assert '--reset-db' in excinfo.value.hint
    finally:
        engine.dispose()


def test_a_database_behind_head_names_both_revisions():
    engine = _engine()
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) '
                              'NOT NULL PRIMARY KEY)'))
            conn.execute(text("INSERT INTO alembic_version VALUES ('0001_initial')"))

        with pytest.raises(SchemaNotReady) as excinfo:
            ensure_schema(engine)

        assert '0001_initial' in excinfo.value.reason
        assert head_revision() in excinfo.value.reason
        assert 'alembic upgrade head' in excinfo.value.hint
    finally:
        engine.dispose()


def test_a_database_at_head_is_accepted():
    engine = _engine()
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) '
                              'NOT NULL PRIMARY KEY)'))
            conn.execute(text('INSERT INTO alembic_version VALUES (:rev)'),
                         {'rev': head_revision()})

        assert ensure_schema(engine) == head_revision()
    finally:
        engine.dispose()


def test_the_app_startup_hook_no_longer_creates_tables():
    """AST, not text: a commented-out call or a mention in a docstring must not pass.

    This is the guard for the defect itself. `scripts/bootstrap_dev.py` already had the
    equivalent assertion; the app entry point did not, which is how it kept calling
    `create_all` for another release after the bootstrap stopped.
    """
    tree = ast.parse((ROOT / 'app' / 'main.py').read_text(encoding='utf-8'))
    offenders = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {'create_all', 'drop_all'}
    ]
    assert offenders == [], (
        'app/main.py must not create or drop tables: the schema belongs to alembic '
        f'(found {offenders}). Use app.schema_guard.ensure_schema instead.'
    )


def test_no_runtime_module_creates_or_drops_tables():
    """`app/`, `worker/` and `scripts/` must never create or drop tables.

    The startup hook and the worker each called `create_all`; both are gone now. This sweeps
    the whole runtime surface so the next module cannot reintroduce it, and it is an AST walk
    rather than a text search so a mention in a docstring or a comment does not count.

    `tests/` and `evaluation/` are deliberately excluded: they build throwaway in-memory
    databases that are never migrated, which is a different thing from shipping a schema.
    """
    offenders: list[str] = []
    for folder in ('app', 'worker', 'scripts'):
        for path in sorted((ROOT / folder).rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {'create_all', 'drop_all'}):
                    offenders.append(f'{path.relative_to(ROOT)}:{node.lineno}')

    assert offenders == [], (
        'the schema belongs to alembic; these call create_all/drop_all: '
        f'{offenders}. Use app.schema_guard.ensure_schema.'
    )
