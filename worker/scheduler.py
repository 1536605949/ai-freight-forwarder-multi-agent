"""Background worker: polls for due follow-up tasks.

Run continuously:
    python -m worker.scheduler

Run once and exit (for a cron / systemd-timer deployment):
    python -m worker.scheduler --once

The poll body is separated from the loop so it can be tested without waiting on a real timer,
and so `--once` reuses exactly the same code path as the daemon.
"""
from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.db import SessionLocal, engine
from app.schema_guard import ensure_schema
from app.logging_utils import configure_logging, log_event
from app.services.followup import process_due
from app.services.sourcing import run_due_sources


async def run_once(db) -> list:
    """One poll. Returns the ids of the follow-up tasks that were processed."""
    ids = await process_due(db)
    if ids:
        log_event('followup.processed', ids=ids)
    return ids


async def run_sources_once(db) -> list[str]:
    """One collection poll for every configured prospecting source.

    Kept separate from `run_once` so a slow directory fetch cannot delay follow-ups, and so
    the follow-up contract (`run_once` returns task ids) stays unchanged.
    """
    ran = await run_due_sources(db)
    if ran:
        log_event('sources.collected', sources=ran)
    return ran


async def main(iterations: int | None = None) -> None:
    """Poll until stopped, or for `iterations` polls when given."""
    configure_logging()
    # The worker refuses to start on a schema alembic does not own. It used to call
    # create_all, which meant starting the worker before the migration produced a database
    # that `alembic upgrade head` could not adopt.
    ensure_schema(engine)
    settings = get_settings()
    log_event('worker.started', poll_seconds=settings.worker_poll_seconds,
              iterations=iterations or 'forever')

    completed = 0
    while iterations is None or completed < iterations:
        db = SessionLocal()
        try:
            await run_once(db)
            # Collection runs after follow-ups so a slow third-party page never delays a
            # customer-facing task. Both share one session and one error boundary.
            await run_sources_once(db)
        except Exception as exc:
            # One bad task must not kill the worker: the loop is the only thing keeping
            # follow-ups flowing, so a failure is logged and the next poll proceeds.
            log_event('worker.error', error=str(exc))
        finally:
            db.close()
        completed += 1
        if iterations is None or completed < iterations:
            await asyncio.sleep(settings.worker_poll_seconds)

    log_event('worker.stopped', polls=completed)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='Follow-up scheduler worker.')
    ap.add_argument('--once', action='store_true',
                    help='run a single poll and exit (for cron / systemd timers)')
    return ap.parse_args(argv)


if __name__ == '__main__':
    args = _parse_args()
    asyncio.run(main(iterations=1 if args.once else None))
