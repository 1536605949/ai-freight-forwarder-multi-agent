"""The acquisition graph: discover, score, decide who is reachable -- and stop there.

Two properties are being pinned:

* when nobody is reachable, the run says *why* instead of returning a silent zero;
* the graph cannot contact anyone. It produces candidates; sending is a separate, approved
  action with its own four gates in `services/prospecting.send_outreach`.

The second one is the reason this is a graph and not a script: a graph that could find a
stranger and email them in one step is exactly what the harness exists to prevent.
"""
from __future__ import annotations

import pytest

from app.graph import run_acquisition
from app.graph.acquisition import NODES
from app.models import AcquisitionRun, GraphCheckpoint, OutreachActivity, ProspectCandidate


def _prospect(db, *, pid: str, email: str | None, score: float = 90.0,
              consent: str = 'unknown', company: str = 'Ningbo Reachable Co') -> None:
    db.add(ProspectCandidate(
        id=pid, tenant_id='tenant-demo', company=company, country='CN',
        trade_lane='Shanghai -> Los Angeles', source='mock', source_ref=pid,
        data_origin='mock_synthetic', status='imported', score=score,
        email=email, consent_status=consent,
    ))
    db.commit()


@pytest.mark.asyncio
async def test_an_empty_source_list_stops_and_says_so(db):
    """'Nothing configured' and 'nothing new' must not look the same."""
    result = await run_acquisition(db, 'tenant-demo', 'u', 't', sources='')

    assert result['status'] == 'no_source_configured'
    assert result['stop_reason'] == 'no_source_configured'
    assert [s['node'] for s in result['steps']] == ['collect']


@pytest.mark.asyncio
async def test_a_source_without_emails_reports_why_nobody_is_reachable(db):
    """The honest outcome for customs-style data: 50 companies, 0 contactable, and a count.

    This is the case that matters commercially. A run that reported "0 prospects" would be
    read as a broken source; the breakdown is what tells the operator the data is fine and
    the contact details are the missing piece.
    """
    result = await run_acquisition(db, 'tenant-demo', 'u', 't', sources='mock|acq|')

    assert result['status'] == 'no_reachable_prospect'
    assert result['accepted'] > 0, 'the mock source should have produced candidates'
    assert result['reachable'] == 0
    assert result['gaps']['no_email'] > 0, f"gaps must explain the zero: {result['gaps']}"
    assert [s['node'] for s in result['steps']] == list(NODES)


@pytest.mark.asyncio
async def test_the_graph_never_contacts_anyone(db):
    await run_acquisition(db, 'tenant-demo', 'u', 't', sources='mock|acq|')

    assert db.query(OutreachActivity).count() == 0, (
        'the acquisition graph produced outreach rows; it must only produce candidates')


@pytest.mark.asyncio
async def test_qualify_selects_candidates_that_are_both_reachable_and_worth_contacting(db):
    _prospect(db, pid='pc_good', email='buyer@reachable.example', score=88)
    _prospect(db, pid='pc_low', email='buyer@lowscore.example', score=10)

    result = await run_acquisition(db, 'tenant-demo', 'u', 't', sources='',
                                   start_node='qualify')

    assert result['status'] == 'outreach_ready'
    ids = [c['prospect_id'] for c in result['candidates']]
    assert ids == ['pc_good'], f'a low-scoring lead must not be offered for contact: {ids}'
    assert result['gaps']['below_floor'] == 1


@pytest.mark.asyncio
async def test_qualify_excludes_suppressed_contacts_regardless_of_score(db):
    _prospect(db, pid='pc_out', email='gone@reachable.example', score=99,
              consent='opted_out', company='Opted Out Co')

    result = await run_acquisition(db, 'tenant-demo', 'u', 't', sources='',
                                   start_node='qualify')

    assert result['status'] == 'no_reachable_prospect'
    assert result['gaps']['suppressed'] == 1
    assert result['candidates'] == []


@pytest.mark.asyncio
async def test_the_run_row_and_its_checkpoints_share_a_thread(db):
    result = await run_acquisition(db, 'tenant-demo', 'u', 't', sources='',
                                   start_node='qualify')

    row = db.query(AcquisitionRun).filter_by(id=result['run_id']).first()
    assert row is not None
    assert row.thread_id == result['thread_id']
    assert row.status == result['status']
    assert row.steps == result['steps']

    snapshots = db.query(GraphCheckpoint).filter_by(thread_id=result['thread_id']).count()
    assert snapshots > 0, 'the acquisition run must be replayable like the quote graph'


@pytest.mark.asyncio
async def test_an_unknown_start_node_is_rejected(db):
    with pytest.raises(ValueError, match='unknown_node'):
        await run_acquisition(db, 'tenant-demo', 'u', 't', start_node='email_everyone')
