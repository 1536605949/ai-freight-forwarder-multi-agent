"""The migration is the source of truth for the schema, so it is tested against the models.

`migrations/versions/0001_initial.py` used to call `Base.metadata.create_all()` and
`drop_all()`. That is not a migration: it introspects the *live* models, so editing a model
silently changes what the same revision produces. Two databases migrated to `0001_initial`
could disagree while both reported success, and the original schema was no longer
recoverable from the code.

The migration is now an explicit, frozen snapshot. These tests hold it to that:

* upgrading an empty database to head must produce exactly the schema the ORM declares
  (tables, columns, nullability, primary keys, unique constraints),
* downgrading to base must leave nothing behind,
* and the migration must not reintroduce a metadata call (AST-checked, so a mention in a
  comment or docstring does not trip it).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.db import Base

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'migrations' / 'versions' / '0001_initial.py'

# SQLite reflects `sa.String(64)` back as VARCHAR, so types are compared by family.
_TYPE_FAMILY = {
    'VARCHAR': 'STRING', 'CHAR': 'STRING', 'TEXT': 'TEXT', 'INTEGER': 'INTEGER',
    'FLOAT': 'FLOAT', 'REAL': 'FLOAT', 'NUMERIC': 'FLOAT', 'DATETIME': 'DATETIME',
    'BOOLEAN': 'BOOLEAN', 'JSON': 'JSON',
}


def _family(type_) -> str:
    return _TYPE_FAMILY.get(type(type_).__name__.upper(), type(type_).__name__.upper())


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Alembic pointed at a throwaway file database."""
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv('DATABASE_URL', url)

    from app.config import get_settings
    get_settings.cache_clear()

    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(ROOT / 'migrations'))
    yield cfg, url

    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The migration must be a snapshot, not an introspection
# --------------------------------------------------------------------------- #

def test_migration_does_not_call_metadata_create_or_drop_all():
    """AST-checked, so prose in the docstring is not mistaken for a call."""
    tree = ast.parse(MIGRATION.read_text(encoding='utf-8'))
    offenders = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {'create_all', 'drop_all'}
    ]
    assert not offenders, f'migration must not call {offenders}; write the DDL out explicitly'


def test_migration_declares_both_directions():
    tree = ast.parse(MIGRATION.read_text(encoding='utf-8'))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {'upgrade', 'downgrade'} <= defined


# --------------------------------------------------------------------------- #
# upgrade head == the ORM schema
# --------------------------------------------------------------------------- #

def test_upgrade_head_matches_the_orm_models(alembic_cfg):
    cfg, url = alembic_cfg
    command.upgrade(cfg, 'head')

    engine = create_engine(url)
    insp = inspect(engine)

    actual_tables = set(insp.get_table_names()) - {'alembic_version'}
    expected_tables = set(Base.metadata.tables)
    assert actual_tables == expected_tables, (
        f'only in migration: {sorted(actual_tables - expected_tables)}; '
        f'only in models: {sorted(expected_tables - actual_tables)}')

    problems: list[str] = []
    for name in sorted(expected_tables):
        model = Base.metadata.tables[name]
        columns = {c['name']: c for c in insp.get_columns(name)}

        for col in model.columns:
            if col.name not in columns:
                problems.append(f'{name}.{col.name}: missing from migration')
                continue
            if columns[col.name]['nullable'] != bool(col.nullable):
                problems.append(
                    f'{name}.{col.name}: nullable={columns[col.name]["nullable"]} '
                    f'in migration, {bool(col.nullable)} in model')
            if _family(columns[col.name]['type']) != _family(col.type):
                problems.append(
                    f'{name}.{col.name}: type {_family(columns[col.name]["type"])} '
                    f'in migration, {_family(col.type)} in model')

        for extra in set(columns) - {c.name for c in model.columns}:
            problems.append(f'{name}.{extra}: in migration but not in model')

        pk = set(insp.get_pk_constraint(name)['constrained_columns'] or [])
        model_pk = {c.name for c in model.columns if c.primary_key}
        if pk != model_pk:
            problems.append(f'{name}: primary key {sorted(pk)} != {sorted(model_pk)}')

        def _uq(inspector):
            return {frozenset(u['column_names']) for u in inspector.get_unique_constraints(name)}
        if _uq(insp) != _uq_from_model(model):
            problems.append(f'{name}: unique constraints differ')

    engine.dispose()
    assert not problems, 'schema drift between migration and models:\n  ' + '\n  '.join(problems)


def _uq_from_model(table) -> set[frozenset[str]]:
    from sqlalchemy import UniqueConstraint
    return {frozenset(c.name for c in con.columns)
            for con in table.constraints if isinstance(con, UniqueConstraint)}


def test_downgrade_base_removes_everything(alembic_cfg):
    """The old `downgrade()` dropped the whole database; this one drops only what it made."""
    cfg, url = alembic_cfg
    command.upgrade(cfg, 'head')
    command.downgrade(cfg, 'base')

    engine = create_engine(url)
    remaining = set(inspect(engine).get_table_names()) - {'alembic_version'}
    engine.dispose()
    assert remaining == set(), f'downgrade left tables behind: {sorted(remaining)}'


def test_upgrade_is_reproducible(alembic_cfg):
    """Two independent upgrades to head must agree -- the property create_all destroyed."""
    cfg, url = alembic_cfg
    command.upgrade(cfg, 'head')
    engine = create_engine(url)
    first = _schema_fingerprint(engine)
    engine.dispose()

    # Downgrade and re-upgrade in place; the result must be byte-identical.
    command.downgrade(cfg, 'base')
    command.upgrade(cfg, 'head')
    engine = create_engine(url)
    second = _schema_fingerprint(engine)
    engine.dispose()

    assert first == second


def _schema_fingerprint(engine) -> dict[str, list[tuple]]:
    insp = inspect(engine)
    out: dict[str, list[tuple]] = {}
    for name in sorted(set(insp.get_table_names()) - {'alembic_version'}):
        cols = sorted((c['name'], str(_family(c['type'])), bool(c['nullable']))
                      for c in insp.get_columns(name))
        out[name] = cols
    return out


# --------------------------------------------------------------------------- #
# The bootstrap must not become a second source of truth for the schema
# --------------------------------------------------------------------------- #

BOOTSTRAP = ROOT / 'scripts' / 'bootstrap_dev.py'


def _call_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def test_bootstrap_never_creates_the_schema_itself():
    """`create_all` here would produce a database alembic cannot adopt.

    The bootstrap used to call `Base.metadata.create_all()`, which builds every table but
    writes no `alembic_version` row. The local database then looked healthy while the
    next `alembic upgrade head` -- the production path -- died with
    `table agent_runs already exists`. AST-checked so the docstring explaining this is
    not mistaken for a call.
    """
    offenders = [n for n in _call_names(BOOTSTRAP) if n == 'create_all']
    assert not offenders, (
        'bootstrap_dev.py must build the schema with `alembic upgrade head`, not '
        'create_all -- otherwise a freshly bootstrapped database cannot be migrated'
    )


def test_bootstrap_drives_the_schema_through_alembic():
    """The upgrade must actually be invoked, not merely mentioned."""
    tree = ast.parse(BOOTSTRAP.read_text(encoding='utf-8'))
    # The command is an argv *list* argument, so the constants live inside an ast.List
    # rather than directly in node.args.
    argv_lists = [
        [el.value for el in arg.elts if isinstance(el, ast.Constant)]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'run'
        for arg in node.args
        if isinstance(arg, ast.List)
    ]
    assert any({'alembic', 'upgrade', 'head'} <= set(argv) for argv in argv_lists), (
        f'expected bootstrap_dev.py to shell out to `alembic upgrade head`; '
        f'saw {argv_lists}'
    )


def test_alembic_revision_reports_nothing_for_a_database_alembic_does_not_own(
    tmp_path, monkeypatch,
):
    """A `create_all` database has no revision, which is what triggers the diagnosis."""
    from scripts.bootstrap_dev import _alembic_revision

    url = f'sqlite:///{(tmp_path / "legacy.db").as_posix()}'
    monkeypatch.setenv('DATABASE_URL', url)

    engine = create_engine(url)
    Base.metadata.create_all(engine)          # stands in for the old bootstrap
    engine.dispose()

    assert _alembic_revision(Path(sys.executable)) is None


def test_alembic_revision_reports_the_revision_once_migrated(alembic_cfg):
    from scripts.bootstrap_dev import _alembic_revision

    cfg, _url = alembic_cfg
    command.upgrade(cfg, 'head')

    assert _alembic_revision(Path(sys.executable)) == '0001_initial'

