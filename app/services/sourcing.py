"""Scheduled prospecting: pull one page, store it, remember where we got to.

The shape of this module follows one rule: **the position of a crawl is state, and state
belongs in the database.** Keeping the cursor in a process variable would mean every worker
restart re-reads page 1, and the dedupe layer would then report `accepted=0` forever -- a
job that looks healthy and produces nothing. That failure is silent, which is why the run
log is a table rather than a log line.

What this module does *not* do:

  - It does not decide who may be emailed. Rows land as `status='imported'`, `score=0`, and
    the send-time gates in `services.prospecting` still apply.
  - It does not retry forever. A failed run leaves its cursor untouched so the next poll
    re-reads the same page, and the error stays visible in `source_runs`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import log_event
from app.models import SourceRun, Tenant
from app.providers.prospecting import build_source
from app.services.common import uid
from app.services.prospecting import import_prospects

# How the row got here, which is what a privacy audit asks first.
_ORIGIN = {
    'mock': 'mock_synthetic',
    'directory': 'public_directory',
    'licensed_vendor': 'licensed_vendor',
}


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    name: str
    url: str | None = None
    lane: str | None = None


def parse_sources(raw: str) -> list[SourceSpec]:
    """Parse `kind|name|url[|lane]` entries, comma separated.

    A malformed entry is kept as-is rather than dropped: `build_source` then raises a named
    error at run time, which an operator can act on. Silently skipping it would leave a
    configured source that never runs and never complains.
    """
    specs: list[SourceSpec] = []
    for chunk in (raw or '').split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split('|')]
        kind = parts[0]
        name = parts[1] if len(parts) > 1 and parts[1] else kind
        url = parts[2] if len(parts) > 2 and parts[2] else None
        lane = parts[3] if len(parts) > 3 and parts[3] else None
        specs.append(SourceSpec(kind=kind, name=name, url=url, lane=lane))
    return specs


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for `DateTime(timezone=True)`."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _last_run(db: Session, tenant_id: str, source: str) -> SourceRun | None:
    return (db.query(SourceRun)
            .filter(SourceRun.tenant_id == tenant_id, SourceRun.source == source)
            .order_by(SourceRun.started_at.desc())
            .first())


def _last_success(db: Session, tenant_id: str, source: str) -> SourceRun | None:
    return (db.query(SourceRun)
            .filter(SourceRun.tenant_id == tenant_id, SourceRun.source == source,
                    SourceRun.status.in_(('ok', 'exhausted')))
            .order_by(SourceRun.started_at.desc())
            .first())


def _resume_cursor(db: Session, tenant_id: str, source: str, *, restart: bool) -> str | None:
    """Where the next fetch starts.

    An `exhausted` source restarts from the top, because a directory grows and the whole
    point of a periodic re-read is finding the companies that were not there last week.
    """
    last = _last_success(db, tenant_id, source)
    if last is None or restart:
        return None
    return last.cursor_after


async def run_source(db: Session, tenant_id: str, actor: str, spec: SourceSpec, *,
                     limit: int | None = None, restart: bool = False) -> dict:
    """Fetch one page from one source and import it.

    Returns a dict rather than raising, because a scheduled job that raises on one bad
    source would skip every source after it. The failure is recorded and reported.
    """
    settings = get_settings()
    limit = limit or settings.prospecting_source_page_size

    if _is_still_running(db, tenant_id, spec.name, settings.prospecting_run_stale_minutes):
        return {'source': spec.name, 'kind': spec.kind, 'skipped': 'previous_run_still_running'}

    cursor = _resume_cursor(db, tenant_id, spec.name, restart=restart)
    run = SourceRun(id=uid('src_'), tenant_id=tenant_id, source=spec.name, kind=spec.kind,
                    status='running', cursor_before=cursor)
    db.add(run)
    db.commit()

    try:
        source = build_source(spec.kind, spec.name, spec.url)
        result = await source.fetch(cursor=cursor, limit=limit, lane=spec.lane)
    except Exception as exc:  # noqa: BLE001 - every failure mode is reported, none swallowed
        run.status = 'failed'
        run.error = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        log_event('source.failed', source=spec.name, kind=spec.kind, error=str(exc))
        return {'source': spec.name, 'kind': spec.kind, 'run_id': run.id,
                'status': 'failed', 'error': str(exc), 'cursor': cursor}

    rows = [r.as_row() for r in result.records if r.company]
    imported = import_prospects(
        db, tenant_id, actor, source=spec.name,
        data_origin=_ORIGIN.get(spec.kind, 'public_directory'),
        rows=rows, default_lane=spec.lane,
    )

    run.status = 'exhausted' if result.exhausted else 'ok'
    run.cursor_after = result.next_cursor
    run.fetched = len(result.records)
    run.accepted = imported['accepted']
    run.rejected = imported['rejected']
    run.note = result.note
    run.finished_at = datetime.now(timezone.utc)
    db.commit()

    log_event('source.collected', source=spec.name, kind=spec.kind, fetched=len(result.records),
              accepted=imported['accepted'], rejected=imported['rejected'],
              exhausted=result.exhausted, note=result.note)

    return {
        'source': spec.name, 'kind': spec.kind, 'run_id': run.id, 'status': run.status,
        'cursor_before': cursor, 'cursor_after': run.cursor_after,
        'fetched': len(result.records), 'accepted': imported['accepted'],
        'rejected': imported['rejected'], 'reasons': imported['reasons'],
        'contact_gaps': imported['contact_gaps'], 'note': result.note,
        'import_note': imported['note'],
    }


def _is_still_running(db: Session, tenant_id: str, source: str, stale_minutes: int) -> bool:
    """Not a lock. It only stops a slow run from stacking another on top of it.

    Multi-replica deployments still need a real lease; this check is single-process
    best-effort and is documented as such in docs/PRODUCTION_GAPS.md.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    recent = (db.query(SourceRun)
              .filter(SourceRun.tenant_id == tenant_id, SourceRun.source == source,
                      SourceRun.status == 'running')
              .order_by(SourceRun.started_at.desc())
              .first())
    return bool(recent and _aware(recent.started_at) and _aware(recent.started_at) > cutoff)


def _is_due(db: Session, tenant_id: str, spec: SourceSpec, *, now: datetime) -> tuple[bool, bool]:
    """(due, restart). A source with pages left is polled on the short interval; one that
    ran out is re-read from the top on the long one."""
    settings = get_settings()
    last = _last_run(db, tenant_id, spec.name)
    if last is None:
        return True, False
    if last.status == 'running':
        return False, False
    started = _aware(last.started_at)
    if started is None:
        return True, False
    if last.status == 'exhausted':
        return started + timedelta(hours=settings.prospecting_source_rescan_hours) <= now, True
    return started + timedelta(hours=settings.prospecting_source_interval_hours) <= now, False


async def run_due_sources(db: Session, *, now: datetime | None = None,
                          tenant_id: str | None = None) -> list[str]:
    """Poll every configured source that is due, for every tenant.

    Returns the names that actually ran, so the worker log says what happened rather than
    "0 tasks" whether or not anything was configured.
    """
    settings = get_settings()
    specs = parse_sources(settings.prospecting_sources)
    if not specs:
        return []

    now = now or datetime.now(timezone.utc)
    tenants = [tenant_id] if tenant_id else [t.id for t in db.query(Tenant).all()]
    ran: list[str] = []

    for tid in tenants:
        for spec in specs:
            due, restart = _is_due(db, tid, spec, now=now)
            if not due:
                continue
            try:
                result = await run_source(db, tid, 'worker:scheduler', spec, restart=restart)
            except Exception as exc:  # noqa: BLE001 - one broken source must not stop the rest
                log_event('source.crashed', source=spec.name, tenant=tid, error=str(exc))
                ran.append(f'{tid}:{spec.name}:crashed')
                continue
            if 'skipped' not in result:
                ran.append(f'{tid}:{spec.name}')
    return ran
