"""The worker is the only thing keeping follow-ups flowing, so its loop is tested.

The poll body used to be inline in an infinite `while True`, which made it untestable and
left the module at 0% coverage. It is now `run_once()`, and `main(iterations=n)` reuses
exactly that path, so the daemon and the `--once` cron mode cannot diverge.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Customer, FollowUpTask
from worker import scheduler


@pytest.mark.asyncio
async def test_run_once_processes_a_due_task(db):
    db.add(Customer(id='cus_w', tenant_id='tenant-demo', company='Acme', email='a@acme.test'))
    db.add(FollowUpTask(id='ft_1', tenant_id='tenant-demo', customer_id='cus_w',
                        due_at=datetime.now(timezone.utc) - timedelta(hours=1),
                        kind='sales_followup', status='pending'))
    db.commit()

    ids = await scheduler.run_once(db)
    assert ids == ['ft_1']
    assert db.query(FollowUpTask).filter_by(id='ft_1').one().status == 'completed'


@pytest.mark.asyncio
async def test_run_once_is_quiet_when_nothing_is_due(db):
    assert await scheduler.run_once(db) == []


@pytest.mark.asyncio
async def test_run_once_does_not_swallow_errors(db, monkeypatch):
    """`run_once` must propagate; the loop is what decides to keep going."""
    async def boom(_db):
        raise RuntimeError('downstream exploded')

    monkeypatch.setattr(scheduler, 'process_due', boom)
    with pytest.raises(RuntimeError):
        await scheduler.run_once(db)


# --------------------------------------------------------------------------- #
# The loop itself
# --------------------------------------------------------------------------- #

@pytest.fixture
def loop_harness(db, monkeypatch):
    """Run `main()` against the test session, with the sleep removed."""
    monkeypatch.setattr(scheduler, 'SessionLocal', lambda: db)
    monkeypatch.setattr(scheduler.Base.metadata, 'create_all', lambda *a, **k: None)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(scheduler.asyncio, 'sleep', fake_sleep)
    return sleeps


@pytest.mark.asyncio
async def test_main_stops_after_the_requested_number_of_polls(db, loop_harness):
    calls: list[int] = []

    async def counting(_db):
        calls.append(1)
        return []

    monkeypatch_target = scheduler.process_due
    scheduler.process_due = counting
    try:
        await scheduler.main(iterations=3)
    finally:
        scheduler.process_due = monkeypatch_target

    assert len(calls) == 3
    # It must not sleep after the final poll, or `--once` would hang.
    assert len(loop_harness) == 2


@pytest.mark.asyncio
async def test_main_survives_a_failing_poll_and_keeps_going(db, loop_harness, monkeypatch):
    """A single bad task must not take the worker down."""
    errors: list[dict] = []
    monkeypatch.setattr(scheduler, 'log_event',
                        lambda event, **kw: errors.append({'event': event, **kw}))

    attempts: list[int] = []

    async def flaky(_db):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('transient')
        return ['ft_9']

    monkeypatch.setattr(scheduler, 'process_due', flaky)
    await scheduler.main(iterations=2)

    assert len(attempts) == 2, 'the second poll must still have run'
    events = [e['event'] for e in errors]
    assert 'worker.error' in events
    assert 'followup.processed' in events
    assert 'worker.started' in events and 'worker.stopped' in events


@pytest.mark.asyncio
async def test_main_closes_its_session_even_when_a_poll_fails(db, loop_harness, monkeypatch):
    closed: list[bool] = []
    monkeypatch.setattr(scheduler, 'SessionLocal',
                        lambda: _ClosingSession(db, closed))
    monkeypatch.setattr(scheduler, 'log_event', lambda event, **kw: None)

    async def boom(_db):
        raise RuntimeError('nope')

    monkeypatch.setattr(scheduler, 'process_due', boom)
    await scheduler.main(iterations=1)
    assert closed == [True], 'a leaked session would exhaust the pool over time'


class _ClosingSession:
    """Wraps the test session to record that `close()` was called."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def close(self):
        self._log.append(True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_once_flag_requests_a_single_poll():
    assert scheduler._parse_args(['--once']).once is True
    assert scheduler._parse_args([]).once is False
