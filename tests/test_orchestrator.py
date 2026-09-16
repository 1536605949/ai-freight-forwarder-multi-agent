"""Orchestrator invariants.

The orchestrator's job is to *sequence* work, not to *grant permission*. The tests below
exist to make that distinction executable:

* the pipeline must run straight through the reversible stages,
* it must STOP at the human gate and report `waiting_approval`,
* it must never send a quote or create a booking on its own,
* and a tenant must not be able to orchestrate another tenant's inquiry.

If a future refactor makes the pipeline "helpfully" continue past a gate, these fail.
"""
import pytest

from app.models import Booking, CustomerQuote, Inquiry, SupplierQuote
from app.services import orchestrator as orch


async def _make_inquiry(db, tenant_id='tenant-demo', complete=True):
    q = Inquiry(
        id='inq_orch', tenant_id=tenant_id, customer_id='cus_orch', raw_message='x',
        origin='Shanghai' if complete else None,
        destination='Los Angeles' if complete else None,
        equipment='40HQ' if complete else None,
        quantity=1, etd='2026-09-25' if complete else None,
        missing_fields=[] if complete else ['etd'],
        status='ready' if complete else 'needs_clarification', external_message_id='m1',
    )
    from app.models import Customer
    db.add(Customer(id='cus_orch', tenant_id=tenant_id, company='Acme', email='a@acme-import.com'))
    db.add(q)
    db.commit()
    return q


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_pipeline_stops_at_the_human_gate(db):
    """The core promise: automation halts before anything customer-facing happens."""
    await _make_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')

    assert run.status == 'waiting_approval'
    stages = [s.stage for s in run.trace]
    assert 'hitl_review' in stages
    # It must have done the reversible work before stopping...
    for done in ('parse', 'schedules', 'rates', 'rfq', 'supplier_quotes', 'optimize'):
        assert done in stages, f'{done} should have run before the gate'
    # ...and it must NOT have done the irreversible work.
    assert 'quote_send' not in stages, 'pipeline must not send a quote without approval'
    assert 'booking' not in stages, 'pipeline must not book without approval'


@pytest.mark.asyncio
async def test_pipeline_creates_no_customer_facing_side_effects_before_the_gate(db):
    await _make_inquiry(db)
    await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')

    cq = db.query(CustomerQuote).filter_by(tenant_id='tenant-demo', inquiry_id='inq_orch').first()
    assert cq is not None, 'a quote should exist for review'
    assert cq.status == 'waiting_approval', 'and it must be un-approved'
    assert db.query(Booking).filter_by(tenant_id='tenant-demo', inquiry_id='inq_orch').count() == 0


@pytest.mark.asyncio
async def test_pending_payload_names_the_quote_to_review(db):
    await _make_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    assert run.pending.get('quote_id'), 'the caller must be told which object needs review'
    assert run.pending.get('resume_hint'), 'and how to resume'


@pytest.mark.asyncio
async def test_pipeline_completes_when_the_gate_is_explicitly_opened(db):
    """`auto_approve` is a demo/test affordance and must be explicitly requested."""
    await _make_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch',
                                  auto_approve=True, vessel_name='MAEU-VESSEL')
    assert run.status == 'completed'
    stages = [s.stage for s in run.trace]
    for s in ('hitl_review', 'quote_send', 'booking', 'vessel_tracking'):
        assert s in stages, f'{s} should run once the gate is opened'


@pytest.mark.asyncio
async def test_full_run_produces_a_booking(db):
    await _make_inquiry(db)
    await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch',
                            auto_approve=True, vessel_name='MAEU-VESSEL')
    b = db.query(Booking).filter_by(tenant_id='tenant-demo', inquiry_id='inq_orch').first()
    assert b is not None and b.provider_ref


# --------------------------------------------------------------------------- #
# Stage trace contract (the front-end depends on this shape)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_trace_entries_are_renderable(db):
    await _make_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    payload = run.as_dict()
    for entry in payload['trace']:
        assert set(entry) == {'stage', 'status', 'detail', 'outputs'}
        assert entry['status'] in {'ok', 'waiting_approval', 'skipped', 'failed'}
    assert payload['status'] in {'running', 'waiting_approval', 'completed', 'failed'}


@pytest.mark.asyncio
async def test_stage_names_are_stable(db):
    """Renaming a stage breaks the demo page, so the list is pinned."""
    assert orch.STAGES[0] == 'parse'
    assert orch.STAGES[-1] == 'vessel_tracking'
    assert orch.GATED_STAGES == {'hitl_review', 'quote_send'}
    # The gate must come after the work that feeds it and before anything customer-facing.
    assert orch.STAGES.index('optimize') < orch.STAGES.index('hitl_review')
    assert orch.STAGES.index('hitl_review') < orch.STAGES.index('booking')


@pytest.mark.asyncio
async def test_gated_and_auto_stages_partition_correctly(db):
    assert orch.GATED_STAGES <= set(orch.STAGES)
    assert orch.AUTO_STAGES <= set(orch.STAGES)
    assert not (orch.GATED_STAGES & orch.AUTO_STAGES), 'a stage cannot be both auto and gated'


# --------------------------------------------------------------------------- #
# Incomplete inquiries
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_incomplete_inquiry_stops_at_parse(db):
    await _make_inquiry(db, complete=False)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    assert run.status == 'waiting_approval'
    assert run.trace[-1].stage == 'parse'
    assert run.trace[-1].outputs.get('missing_fields') == ['etd']


# --------------------------------------------------------------------------- #
# Tenant isolation - a gate that a bug could open across tenants
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_cannot_orchestrate_another_tenants_inquiry(db):
    await _make_inquiry(db, tenant_id='tenant-other')
    with pytest.raises(KeyError):
        await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')


@pytest.mark.asyncio
async def test_status_cannot_leak_across_tenants(db):
    await _make_inquiry(db, tenant_id='tenant-other')
    with pytest.raises(KeyError):
        orch.pipeline_status(db, 'tenant-demo', 'inq_orch')
    # The owning tenant can read it.
    assert orch.pipeline_status(db, 'tenant-other', 'inq_orch')['inquiry_id'] == 'inq_orch'


# --------------------------------------------------------------------------- #
# Idempotency / resume
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resume_after_rejection_reports_failure(db):
    await _make_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    quote_id = run.pending['quote_id']

    from app.services.approval import review_quote
    review_quote(db, 'tenant-demo', 'reviewer', quote_id, 'reject', 'too cheap')

    resumed = await orch.resume_after_approval(db, 'tenant-demo', 'reviewer', 't', quote_id)
    assert resumed.status == 'failed'
    assert 'quote_rejected' in resumed.trace[-1].detail


@pytest.mark.asyncio
async def test_resume_after_approval_sends_then_books(db):
    await _make_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    quote_id = run.pending['quote_id']

    from app.services.approval import review_quote
    review_quote(db, 'tenant-demo', 'reviewer', quote_id, 'approve', 'ok')

    resumed = await orch.resume_after_approval(db, 'tenant-demo', 'reviewer', 't', quote_id)
    assert resumed.status == 'completed'
    stages = [s.stage for s in resumed.trace]
    assert 'quote_send' in stages and 'booking' in stages


@pytest.mark.asyncio
async def test_rerun_does_not_open_a_second_rfq(db):
    """Re-running must be safe: one inquiry, one RFQ."""
    from app.models import RFQ
    await _make_inquiry(db)
    await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    assert db.query(RFQ).filter_by(tenant_id='tenant-demo', inquiry_id='inq_orch').count() == 1


# --------------------------------------------------------------------------- #
# Status snapshot
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_status_reports_what_it_is_waiting_on(db):
    await _make_inquiry(db)
    assert orch.pipeline_status(db, 'tenant-demo', 'inq_orch')['waiting_on'] == 'supplier_quotes'
    await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch')
    assert orch.pipeline_status(db, 'tenant-demo', 'inq_orch')['waiting_on'] == 'human_approval'


@pytest.mark.asyncio
async def test_status_is_side_effect_free(db):
    """Reading status must never create a quote or advance anything."""
    await _make_inquiry(db)
    before = db.query(SupplierQuote).count()
    orch.pipeline_status(db, 'tenant-demo', 'inq_orch')
    orch.pipeline_status(db, 'tenant-demo', 'inq_orch')
    assert db.query(SupplierQuote).count() == before
    assert db.query(CustomerQuote).count() == 0


@pytest.mark.asyncio
async def test_unknown_start_stage_falls_back_to_beginning(db):
    """Defensive: an unknown stage must not silently skip the pipeline."""
    await _make_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_orch', start_stage='nonsense')
    assert run.trace[0].stage == 'parse'
