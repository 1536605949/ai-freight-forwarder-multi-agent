"""One-command local bootstrap for the AI Freight Forwarder demo.

Design goals
------------
* **Idempotent** - safe to run any number of times. Nothing is deleted or
  overwritten; every step checks "is this already done?" first.
* **Cross-platform** - Windows / macOS / Linux. Uses only the stdlib plus the
  interpreter's own venv module; no bash-isms, no `source`.
* **Fail loud, fail actionable** - a non-zero exit code plus a concrete "try
  this next" line. A demo that half-bootstraps silently is worse than one that
  shouts.
* **Offline-safe by default** - `--skip-install` lets you bootstrap a machine
  whose venv already has the deps (air-gapped demo laptop, CI image).

What it does, in order
----------------------
1. Check the interpreter is new enough (>= 3.11, per pyproject.toml).
2. Create `.venv/` if missing.
3. Install `requirements-dev.txt` into it (unless `--skip-install`).
4. Bring the schema to head with `alembic upgrade head` (idempotent, and the same
   code path production uses -- see `init_db` for why it is not `create_all`).
5. Seed `tenant-demo` and print a dev token.
6. Print the exact commands to start the API, worker and demo page.

Usage
-----
    python scripts/bootstrap_dev.py                 # full bootstrap
    python scripts/bootstrap_dev.py --skip-install  # venv already has deps
    python scripts/bootstrap_dev.py --check         # verify only, no changes
    python scripts/bootstrap_dev.py --reset-db      # DROP and recreate tables
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
VENV_DIR: Final = ROOT / ".venv"
MIN_PYTHON: Final = (3, 11)
IS_WINDOWS: Final = platform.system() == "Windows"


# --------------------------------------------------------------------------- #
# Console helpers - deliberately avoid colorama/tqdm so this runs before deps.
# --------------------------------------------------------------------------- #

class Log:
    _quiet = False

    @classmethod
    def step(cls, n: int, total: int, msg: str) -> None:
        if not cls._quiet:
            print(f"\n[{n}/{total}] {msg}")

    @classmethod
    def ok(cls, msg: str) -> None:
        if not cls._quiet:
            print(f"      OK   {msg}")

    @classmethod
    def skip(cls, msg: str) -> None:
        if not cls._quiet:
            print(f"      SKIP {msg}")

    @classmethod
    def warn(cls, msg: str) -> None:
        if not cls._quiet:
            print(f"      WARN {msg}")

    @classmethod
    def info(cls, msg: str) -> None:
        if not cls._quiet:
            print(f"      ..   {msg}")


class BootstrapError(RuntimeError):
    """Raised when bootstrap cannot proceed. Carries a 'what to do next' hint."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------- #
# Step 1 - interpreter check
# --------------------------------------------------------------------------- #

def check_python() -> None:
    Log.step(1, 6, "Checking Python version")
    if sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        got = ".".join(map(str, sys.version_info[:3]))
        raise BootstrapError(
            f"Python {need}+ required, found {got}",
            hint="Install a newer Python and re-run with that interpreter, e.g. "
                 "`py -3.12 scripts/bootstrap_dev.py` on Windows.",
        )
    Log.ok(f"Python {'.'.join(map(str, sys.version_info[:3]))} ({sys.executable})")


# --------------------------------------------------------------------------- #
# Step 2 - virtualenv
# --------------------------------------------------------------------------- #

def venv_python() -> Path:
    """Path to the python executable *inside* .venv, platform-appropriate."""
    if IS_WINDOWS:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv(*, skip_install: bool) -> Path:
    Log.step(2, 6, "Preparing virtualenv (.venv)")
    if venv_python().exists():
        Log.skip(f"already exists: {VENV_DIR}")
        return venv_python()

    # Running inside an existing venv already? Then don't nest one.
    if sys.prefix != sys.base_prefix and not skip_install:
        Log.skip("already running inside a virtualenv, using it")
        return Path(sys.executable)

    Log.info(f"creating {VENV_DIR}")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    except subprocess.CalledProcessError as exc:
        raise BootstrapError(
            "`python -m venv` failed",            hint="On Debian/Ubuntu install the venv package: "
                 "`sudo apt install python3-venv`.",
        ) from exc

    if not venv_python().exists():
        raise BootstrapError(
            f"venv created but {venv_python()} is missing",
            hint="Delete .venv/ and retry; if it persists your Python install "
                 "may be missing the `ensurepip` module.",
        )
    Log.ok(f"created {VENV_DIR}")
    return venv_python()


# --------------------------------------------------------------------------- #
# Step 3 - dependencies
# --------------------------------------------------------------------------- #

def install_deps(py: Path, *, skip_install: bool) -> None:
    Log.step(3, 6, "Installing dependencies")
    if skip_install:
        Log.skip("--skip-install given")
        return

    req = ROOT / "requirements-dev.txt"
    if not req.exists():
        raise BootstrapError(f"{req.name} not found", hint="Run this from the repo root.")

    # Upgrade pip quietly; a stale pip is the most common cause of
    # spurious ResolutionImpossible errors on older machines.
    subprocess.run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip", "-q"],
        check=False,  # non-fatal: some locked-down mirrors forbid self-upgrade
    )

    Log.info(f"pip install -r {req.name}  (first run downloads ~60MB, be patient)")
    result = subprocess.run(
        [str(py), "-m", "pip", "install", "-r", str(req)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise BootstrapError(
            f"dependency installation failed (exit {result.returncode})",
            hint="If you see `ResolutionImpossible`, check that agent-framework "
                 "is NOT in requirements.txt - it belongs in requirements-agent.txt. "
                 "For a corporate mirror add `-i <index-url>`.",
        )
    Log.ok("dependencies installed (mock mode needs no LLM key)")


# --------------------------------------------------------------------------- #
# Step 4 + 5 - database and seed
# --------------------------------------------------------------------------- #

def _load_app_modules(py: Path, script: str) -> subprocess.CompletedProcess:
    """Run a small python snippet *inside the venv* against the app package."""
    return subprocess.run(
        [str(py), "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


def init_db(py: Path, *, reset: bool) -> None:
    """Bring the local schema to head **via alembic**, the same path production uses.

    This deliberately does *not* call ``Base.metadata.create_all``.  ``create_all``
    builds the tables but never writes an ``alembic_version`` row, so the database is
    left in a state the migration system cannot adopt: the next
    ``alembic upgrade head`` dies with ``table agent_runs already exists``.  Two
    sources of truth for the schema is one too many -- and the one that drifts
    silently is always the one you did not test.

    ``migrations/env.py`` resolves its URL from ``get_settings().database_url``, so
    this touches exactly the database the app will use.
    """
    Log.step(4, 6, "Creating database schema (alembic)")

    if reset:
        drop = """
import sys
sys.path.insert(0, '.')
from sqlalchemy import text
from app.db import Base, engine
import app.models  # noqa: F401 - register all mappers before drop_all
Base.metadata.drop_all(engine)
# alembic_version is alembic's own bookkeeping table, not an ORM model, so drop_all
# does not know about it. Leaving it behind would make the next upgrade a no-op.
with engine.begin() as conn:
    conn.execute(text('DROP TABLE IF EXISTS alembic_version'))
print('dropped')
"""
        proc = _load_app_modules(py, drop)
        if proc.returncode != 0:
            raise BootstrapError(
                "dropping the schema failed (--reset-db)",
                hint=(proc.stderr or "").strip().splitlines()[-1]
                if proc.stderr
                else "Is another process holding the database open?",
            )
        Log.ok("schema dropped (--reset-db)")

    result = subprocess.run(
        [str(py), "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        blob = f"{result.stderr or ''}\n{result.stdout or ''}"
        lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
        # SQLAlchemy signs off with a "Background on this error at: <link>" line, which
        # is never the line that tells you anything.  Prefer the last line that names
        # an error, and match the condition against the whole output rather than the
        # last line -- the useful text is rarely last.
        named = [ln for ln in lines if "error" in ln.lower()]
        last = named[-1] if named else (lines[-1] if lines else "")

        # The one failure worth diagnosing for the user: a database built by an older
        # bootstrap that used create_all, so the tables exist but alembic has no record
        # of having created them.  Stamping is the non-destructive fix, but only the
        # operator can know whether that schema really is at head -- so we say so
        # rather than stamping silently and hiding possible drift.
        if "already exists" in blob.lower() and not _alembic_revision(py):
            raise BootstrapError(
                "this database was created before alembic owned the schema "
                "(its tables exist but there is no alembic_version row)",
                hint="If it holds nothing you need, rebuild it: "
                     "`python scripts/bootstrap_dev.py --skip-install --reset-db`. "
                     "Otherwise, having confirmed the schema is current, adopt it with "
                     "`alembic stamp head`.",
            )
        raise BootstrapError(
            "`alembic upgrade head` failed",
            hint=last or "Run `alembic upgrade head` manually to see why.",
        )

    revision = subprocess.run(
        [str(py), "-m", "alembic", "current"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    at = (revision.stdout or "").strip().splitlines()
    Log.ok(f"alembic upgrade head: at {at[0] if at else 'head'}")


def _alembic_revision(py: Path) -> str | None:
    """The revision alembic has recorded, or ``None`` if it has recorded nothing.

    Two different situations both mean "alembic does not own this schema", and the
    operator cares about the same thing in both:

    * no ``alembic_version`` table at all -- built by an older ``create_all`` bootstrap;
    * the table exists but is empty -- a previous upgrade created its bookkeeping table
      and then died before it could stamp anything.

    Checking for the *row* rather than the *table* is what distinguishes those from a
    database that is genuinely under migration control.
    """
    snippet = """
import sys
sys.path.insert(0, '.')
from sqlalchemy import inspect, text
from app.db import engine
if 'alembic_version' not in inspect(engine).get_table_names():
    print('')
else:
    with engine.connect() as conn:
        row = conn.execute(text('SELECT version_num FROM alembic_version')).fetchone()
    print(row[0] if row else '')
"""
    proc = _load_app_modules(py, snippet)
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines and lines[-1].strip() else None


def seed(py: Path) -> None:
    Log.step(5, 6, "Seeding demo data")
    snippet = """
import sys
sys.path.insert(0, '.')
from app.db import SessionLocal
from app.models import Tenant
db = SessionLocal()
if db.get(Tenant, 'tenant-demo') is None:
    db.add(Tenant(id='tenant-demo', name='Demo Freight Forwarder'))
    db.commit()
    print('created')
else:
    print('exists')
db.close()
"""
    proc = _load_app_modules(py, snippet)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise BootstrapError(
            "seeding failed",
            hint=err[-1] if err else "Check DATABASE_URL permissions.",
        )
    Log.ok(f"tenant-demo {proc.stdout.strip().splitlines()[-1]}")


def make_dev_token(py: Path) -> str | None:
    """Best-effort dev token, so the printed curl commands actually work."""
    if not (ROOT / "scripts" / "dev_token.py").exists():
        return None
    proc = _load_app_modules(py, "import sys; sys.path.insert(0,'.');"
                                 "exec(open('scripts/dev_token.py').read())")
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else None


# --------------------------------------------------------------------------- #
# Step 6 - readiness probe
# --------------------------------------------------------------------------- #

def verify(py: Path) -> bool:
    """Import the app inside the venv. This is the real 'did it work?' check."""
    snippet = """
import sys
sys.path.insert(0, '.')
from app.main import app
from app.agents.runtime import get_runtime
print(len(app.routes))
print(type(get_runtime()).__name__)
"""
    proc = _load_app_modules(py, snippet)
    if proc.returncode != 0:
        Log.warn("app failed to import - bootstrap is incomplete")
        for line in (proc.stderr or "").strip().splitlines()[-6:]:
            print(f"         {line}")
        return False
    lines = proc.stdout.strip().splitlines()
    routes = lines[-2] if len(lines) >= 2 else "?"
    runtime = lines[-1] if lines else "?"
    Log.ok(f"app imports cleanly - {routes} routes, runtime={runtime}")
    return True


# --------------------------------------------------------------------------- #
# Final instructions
# --------------------------------------------------------------------------- #

def print_next_steps(py: Path, *, healthy: bool) -> None:
    py_disp = str(py.relative_to(ROOT)) if py.is_relative_to(ROOT) else str(py)
    act = f".venv{os.sep}Scripts{os.sep}activate" if IS_WINDOWS else "source .venv/bin/activate"
    token = make_dev_token(py)

    print("\n" + "=" * 68)
    print("  Bootstrap complete" if healthy else "  Bootstrap finished WITH WARNINGS")
    print("=" * 68)
    print(f"""
  Activate the venv:
      {act}

  Start the API (terminal 1):
      uvicorn app.main:app --reload --port 8100

  Start the background worker (terminal 2, optional):
      python -m worker.scheduler

  Open the demo pages:
      Prospecting   http://localhost:8100/demo/prospecting.html
      Full pipeline http://localhost:8100/demo/overview.html
      API docs      http://localhost:8100/docs

  Run the checks:
      pytest -q                       # unit + integration
      python scripts/acceptance.py    # end-to-end against a running server
      python scripts/delivery_check.py

  Interpreter used by this bootstrap: {py_disp}""")

    if token:
        print(f"""
  Dev token (AUTH_MODE=dev):
      {token}""")

    print("""
  Mock mode is the default (AGENT_PROVIDER=mock) - no LLM key required.
  To use a real model instead:
      pip install -r requirements-agent.txt
      set AGENT_PROVIDER=maf_openai  (and OPENAI_API_KEY)
""")
    print("=" * 68)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="One-command local bootstrap for the freight forwarder demo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--skip-install", action="store_true",
                   help="Don't touch pip; assume the venv already has dependencies.")
    p.add_argument("--check", action="store_true",
                   help="Verify the current setup without changing anything.")
    p.add_argument("--reset-db", action="store_true",
                   help="DROP all tables before recreating them. Destroys local demo data.")
    p.add_argument("--quiet", action="store_true", help="Only print the final summary.")
    p.add_argument("--json", action="store_true",
                   help="Emit a machine-readable result object on stdout (for CI).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    Log._quiet = args.quiet

    if not args.quiet and not args.json:
        print("=" * 68)
        print("  AI Freight Forwarder - local bootstrap")
        print(f"  repo: {ROOT}")
        print("=" * 68)

    result: dict[str, object] = {"ok": False, "steps": [], "python": str(sys.executable)}

    try:
        check_python()
        result["steps"].append("python")

        py = venv_python() if venv_python().exists() else Path(sys.executable)
        if not args.check:
            py = ensure_venv(skip_install=args.skip_install)
            result["steps"].append("venv")
            install_deps(py, skip_install=args.skip_install)
            result["steps"].append("deps")
            init_db(py, reset=args.reset_db)
            result["steps"].append("db")
            seed(py)
            result["steps"].append("seed")
        else:
            Log.step(2, 6, "Preparing virtualenv")
            Log.skip("--check: no changes made")
            for n, name in ((3, "Installing dependencies"), (4, "Creating database schema"), (5, "Seeding demo data")):
                Log.step(n, 6, name)
                Log.skip("--check: no changes made")
            if not venv_python().exists():
                Log.warn(".venv missing - run without --check to create it")

        Log.step(6, 6, "Verifying")
        healthy = verify(py)
        result["healthy"] = healthy
        result["ok"] = healthy

    except BootstrapError as exc:
        print(f"\n  FAILED: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"  Try:    {exc.hint}", file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc), "hint": exc.hint}))
        return 1
    except KeyboardInterrupt:
        print("\n  Interrupted.", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(result))
    else:
        print_next_steps(py, healthy=result.get("healthy", False))

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
