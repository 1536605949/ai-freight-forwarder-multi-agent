"""The SQL checkpointer: real snapshots, tenant scoping, and a replayable timeline.

The claim being tested is narrow and checkable: after a run, the database holds one state
snapshot per super-step, they are ordered, and they are readable without re-running anything.
That is the difference between "the workflow is recoverable" as a sentence and as a fact.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.graph import run_checkpoints, run_graph
from app.graph.checkpoint import SqlCheckpointSaver, read_history
from app.models import Customer, GraphCheckpoint, Inquiry, PipelineGraphRun


def _seed(db, inquiry_id: str = 'inq_ckpt') -> None:
    db.add(Customer(id=f'cus_{inquiry_id}', tenant_id='tenant-demo', company='Acme',
                    email='a@acme-import.com'))
    db.add(Inquiry(id=inquiry_id, tenant_id='tenant-demo', customer_id=f'cus_{inquiry_id}',
                   raw_message='1x40HQ Shanghai to Los Angeles',
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ',
                   quantity=1, etd='2026-09-25', missing_fields=[], status='ready',
                   external_message_id=f'm-{inquiry_id}'))
    db.commit()


@pytest.mark.asyncio
async def test_a_run_writes_a_snapshot_per_super_step(db):
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_ckpt',
                          allow_synthetic_quotes=True)

    history = read_history(db, run.thread_id)
    assert len(history) > len(run.steps), (
        'there must be more snapshots than trace entries: one per node plus the entry and '
        f'final states; got {len(history)} snapshots for {len(run.steps)} steps')

    # Every node that ran is visible in the cumulative node list of some snapshot.
    seen = history[-1]['nodes']
    assert seen == [s['node'] for s in run.steps]


@pytest.mark.asyncio
async def test_the_timeline_is_ordered_oldest_first_and_ends_at_the_run_status(db):
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_ckpt',
                          allow_synthetic_quotes=True)

    history = read_history(db, run.thread_id)
    assert history[0]['nodes'] == [], 'the first snapshot is the entry state, before any node'
    assert history[-1]['status'] == run.status
    steps_seen = [h['step'] for h in history]
    assert steps_seen == sorted(steps_seen), f'timeline is out of order: {steps_seen}'


@pytest.mark.asyncio
async def test_checkpoints_are_reachable_through_the_run_row_and_tenant_scoped(db):
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_ckpt',
                          allow_synthetic_quotes=True)

    rows = run_checkpoints(db, 'tenant-demo', run.run_id)
    assert rows and rows[-1]['status'] == run.status

    # A thread id is an internal identifier, not a capability: another tenant guessing the
    # run id must not read these snapshots.
    with pytest.raises(KeyError, match='run_not_found'):
        run_checkpoints(db, 'tenant-other', run.run_id)


@pytest.mark.asyncio
async def test_snapshots_survive_a_new_session(db):
    """A deploy between two nodes must not lose the history. That is why this is in SQL."""
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_ckpt',
                          allow_synthetic_quotes=True)

    other = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        assert other.query(GraphCheckpoint).filter_by(thread_id=run.thread_id).count() > 0
        history = read_history(other, run.thread_id)
        assert history[-1]['status'] == run.status
    finally:
        other.close()


@pytest.mark.asyncio
async def test_pending_writes_are_three_tuples(db):
    """Regression: `(channel, value)` type-checks and then fails at replay time.

    `read_task_statuses` unpacks three values. Returning pairs raised
    `not enough values to unpack (expected 3, got 2)` only when the history was rendered,
    which is after the run had already been reported as successful.
    """
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_ckpt',
                          allow_synthetic_quotes=True)

    saver = SqlCheckpointSaver(db)
    config = {'configurable': {'thread_id': run.thread_id}}
    tuples = [t for t in saver.list(config) if t.pending_writes]
    assert tuples, 'no checkpoint carried pending writes; the writes table is not being used'

    for item in tuples:
        for write in item.pending_writes:
            assert len(write) == 3, f'pending write is not (task_id, channel, value): {write!r}'


@pytest.mark.asyncio
async def test_two_runs_do_not_share_a_thread(db):
    """A resumed run must not inherit the previous run's snapshots."""
    _seed(db, 'inq_a')
    first = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_a', allow_synthetic_quotes=True)
    second = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_a', allow_synthetic_quotes=True)

    assert first.thread_id != second.thread_id
    first_history = read_history(db, first.thread_id)
    second_history = read_history(db, second.thread_id)
    assert first_history and second_history
    assert db.query(PipelineGraphRun).filter_by(inquiry_id='inq_a').count() == 2
