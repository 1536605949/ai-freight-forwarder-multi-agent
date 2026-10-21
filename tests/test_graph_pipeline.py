"""Graph orchestration: conditional routing, the gate, and gate-aware resume.

These tests exercise the real LangGraph object (no stub), because the value being tested is
the routing itself: an incomplete inquiry must take the clarification edge, a thin supplier
round must stop before pricing, and a resume must re-enter at the node it stopped at.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.graph import latest_run, resume_graph, run_graph
from app.models import Booking, Customer, CustomerQuote, Inquiry, PipelineGraphRun
from app.services import approval as approval_svc


def _seed(db, *, complete: bool = True, inquiry_id: str = 'inq_graph',
          tenant_id: str = 'tenant-demo') -> Inquiry:
    db.add(Customer(id=f'cus_{inquiry_id}', tenant_id=tenant_id, company='Acme',
                    email=f'{inquiry_id}@acme-import.com'))
    row = Inquiry(
        id=inquiry_id, tenant_id=tenant_id, customer_id=f'cus_{inquiry_id}',
        raw_message='Need 1x40HQ Shanghai to Los Angeles, ETD 2026-09-25',
        origin='Shanghai' if complete else None,
        destination='Los Angeles' if complete else None,
        equipment='40HQ' if complete else None,
        quantity=1,
        etd='2026-09-25' if complete else None,
        missing_fields=[] if complete else ['etd'],
        status='ready' if complete else 'needs_clarification',
        external_message_id=f'm-{inquiry_id}',
    )
    db.add(row)
    db.commit()
    return row


@pytest.mark.asyncio
async def test_graph_stops_at_the_human_gate(db):
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                          allow_synthetic_quotes=True)

    assert run.status == 'waiting_approval'
    nodes = [s['node'] for s in run.steps]
    assert nodes == ['understand', 'schedules', 'rates', 'rfq', 'supplier_quotes',
                     'optimize', 'hitl_review']
    assert 'quote_send' not in nodes
    assert 'booking' not in nodes
    assert run.resume_node == 'quote_send'


@pytest.mark.asyncio
async def test_graph_records_no_customer_facing_side_effect_before_the_gate(db):
    _seed(db)
    await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph', allow_synthetic_quotes=True)

    quote = db.query(CustomerQuote).filter_by(tenant_id='tenant-demo',
                                              inquiry_id='inq_graph').first()
    assert quote is not None and quote.status == 'waiting_approval'
    assert db.query(Booking).filter_by(tenant_id='tenant-demo', inquiry_id='inq_graph').count() == 0


@pytest.mark.asyncio
async def test_incomplete_inquiry_takes_the_clarification_edge(db):
    """The branch is the point: it must not run schedules/rates on a half-specified inquiry."""
    _seed(db, complete=False)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                          allow_synthetic_quotes=True)

    assert run.status == 'waiting_clarification'
    assert run.stop_reason == 'inquiry_incomplete'
    nodes = [s['node'] for s in run.steps]
    assert nodes == ['understand', 'clarify']
    assert 'schedules' not in nodes
    assert run.resume_node == 'understand'


@pytest.mark.asyncio
async def test_a_thin_supplier_round_waits_instead_of_pricing(db):
    """Pricing on a single quote is how a below-cost quote gets sent."""
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                          allow_synthetic_quotes=False)

    assert run.status == 'waiting_supplier_quotes'
    nodes = [s['node'] for s in run.steps]
    assert 'optimize' not in nodes
    assert run.resume_node == 'supplier_quotes'


@pytest.mark.asyncio
async def test_resume_after_approval_sends_and_books(db):
    _seed(db)
    first = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                            allow_synthetic_quotes=True)
    quote = db.query(CustomerQuote).filter_by(id=first.state['quote_id']).first()
    approval_svc.review_quote(db, 'tenant-demo', 'reviewer', quote.id, 'approve', 'ok')

    second = await resume_graph(db, 'tenant-demo', 'u', 't', 'inq_graph')

    assert second.status == 'completed'
    assert [s['node'] for s in second.steps] == ['quote_send', 'booking', 'vessel_tracking']
    assert db.query(Booking).filter_by(tenant_id='tenant-demo', inquiry_id='inq_graph').count() == 1


@pytest.mark.asyncio
async def test_resume_without_approval_fails_at_the_gate_and_does_not_send(db):
    """The service-layer gate must hold even when the graph is driven straight at it."""
    _seed(db)
    await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph', allow_synthetic_quotes=True)

    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                          start_node='quote_send', allow_synthetic_quotes=True)

    assert run.status == 'failed'
    assert 'approval_required' in (run.stop_reason or '')
    quote = db.query(CustomerQuote).filter_by(tenant_id='tenant-demo',
                                              inquiry_id='inq_graph').first()
    assert quote.status == 'waiting_approval'
    assert quote.sent_at is None


@pytest.mark.asyncio
async def test_resume_derives_the_node_from_persisted_state(db):
    _seed(db)
    await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph', allow_synthetic_quotes=True)

    stored = latest_run(db, 'tenant-demo', 'inq_graph')
    assert stored is not None
    assert stored.status == 'waiting_approval'
    assert stored.pending_gate == 'human_review_required'
    assert stored.current_node == 'hitl_review'


@pytest.mark.asyncio
async def test_completed_run_is_not_resumable(db):
    _seed(db)
    first = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                            allow_synthetic_quotes=True)
    approval_svc.review_quote(db, 'tenant-demo', 'reviewer', first.state['quote_id'], 'approve', 'ok')
    await resume_graph(db, 'tenant-demo', 'u', 't', 'inq_graph')

    with pytest.raises(ValueError, match='not_resumable'):
        await resume_graph(db, 'tenant-demo', 'u', 't', 'inq_graph')


@pytest.mark.asyncio
async def test_production_cannot_seed_synthetic_quotes(db, monkeypatch):
    """A real quote comparison must never contain demo supplier quotes."""
    monkeypatch.setenv('ENVIRONMENT', 'production')
    get_settings.cache_clear()
    try:
        _seed(db)
        run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                              allow_synthetic_quotes=True)
        assert run.synthetic_quotes_allowed is False
        assert run.status == 'waiting_supplier_quotes'
    finally:
        monkeypatch.delenv('ENVIRONMENT', raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unknown_start_node_is_rejected(db):
    _seed(db)
    with pytest.raises(ValueError, match='unknown_node'):
        await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph', start_node='send_everything')


@pytest.mark.asyncio
async def test_graph_run_is_persisted_with_a_cursor(db):
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                          allow_synthetic_quotes=True)
    rows = db.query(PipelineGraphRun).filter_by(tenant_id='tenant-demo').all()
    assert len(rows) == 1
    assert rows[0].id == run.run_id
    assert rows[0].thread_id == run.thread_id
    assert rows[0].steps[-1]['node'] == 'hitl_review'


@pytest.mark.asyncio
async def test_a_failed_node_stops_the_run_instead_of_cascading(db):
    """One failure must end the run, and the run must report *that* failure.

    This is a regression test for a defect found by driving the real server, not by the
    suite: `understand` failed with `inquiry_not_found`, and because the next four hops were
    plain edges the graph went on to run schedules, rates, rfq and supplier_quotes, each
    failing in turn. The reported stop reason was the *last* failure
    (`supplier_quotes:rfq_missing`), so an operator would have investigated the RFQ for a
    missing inquiry.

    Two assertions, because the count and the reason are separate failures: running four
    nodes that cannot possibly succeed wastes work and pollutes the trace; reporting the
    wrong cause sends the investigation to the wrong place.
    """
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_does_not_exist')

    assert run.status == 'failed'
    assert [s['node'] for s in run.steps] == ['understand']
    assert run.stop_reason.startswith('understand:'), (
        f'the first failure is the cause; got {run.stop_reason!r}'
    )


@pytest.mark.asyncio
async def test_a_failure_after_a_successful_node_still_stops_the_next_hop(db):
    """The cascade guard is not specific to `understand`.

    `supplier_quotes` is reached through a plain edge from `rfq`; a refusal there must end
    the run before `optimize` prices on zero quotes.
    """
    _seed(db)
    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_graph',
                          allow_synthetic_quotes=False)

    nodes = [s['node'] for s in run.steps]
    assert run.status == 'waiting_supplier_quotes'
    assert nodes == ['understand', 'schedules', 'rates', 'rfq', 'supplier_quotes']
    assert 'optimize' not in nodes
