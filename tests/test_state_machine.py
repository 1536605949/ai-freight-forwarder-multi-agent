"""The state graph is a safety property, so it is tested as one.

Three classes of assertion live here:

1. **Table sanity** - the graph is total over the enum, `closed` is a sink, and no edge
   points at an undeclared status. Cheap, and it catches a half-finished edit.
2. **The reproduced defect** - editing an inquiry must not rewind its status. Before the
   guard, `PATCH /api/v1/inquiries/{id}` on a `booked` inquiry reset it to `ready`.
3. **The pipeline's real path** - the orchestrator is instrumented and its actual sequence of
   transitions is compared against `HAPPY_PATH`. This is what stops a future stage reorder
   from silently producing an impossible history.
"""
import importlib

import pytest

from app import state_machine as sm
from app.enums import InquiryStatus
from app.models import Customer, Inquiry
from app.schemas import InquiryPatch
from app.services import orchestrator as orch
from app.services.inquiry import patch_inquiry

# Modules that advance an inquiry's status. Each holds its own reference to `advance_status`,
# so a spy has to be installed on every one of them.
_ADVANCING_MODULES = [
    'app.services.schedule',
    'app.services.rfq',
    'app.services.pricing',
    'app.services.approval',
    'app.services.booking',
]


# --------------------------------------------------------------------------- #
# 1. Table sanity
# --------------------------------------------------------------------------- #

def test_graph_is_total_over_the_enum():
    """Every declared status has an entry, so a lookup can never KeyError."""
    assert set(sm.INQUIRY_TRANSITIONS) == set(InquiryStatus)


def test_closed_is_the_only_sink():
    assert sm.INQUIRY_TRANSITIONS[InquiryStatus.CLOSED] == frozenset()
    for state, targets in sm.INQUIRY_TRANSITIONS.items():
        if state is not InquiryStatus.CLOSED:
            assert InquiryStatus.CLOSED in targets, f'{state} must be closable'


def test_every_edge_targets_a_declared_status():
    for targets in sm.INQUIRY_TRANSITIONS.values():
        assert targets <= set(InquiryStatus)


def test_graph_is_forward_only():
    """No edge may move to an earlier position on the happy path.

    This is the machine-checked version of "automation must not rewind a commitment".
    """
    order = {s: i for i, s in enumerate(sm.HAPPY_PATH)}
    for state, targets in sm.INQUIRY_TRANSITIONS.items():
        if state not in order:
            continue
        for target in targets:
            if target in order:
                assert order[target] >= order[state], f'{state} -> {target} moves backwards'


def test_happy_path_is_fully_connected():
    for a, b in zip(sm.HAPPY_PATH, sm.HAPPY_PATH[1:]):
        assert sm.can_transition(a, b), f'{a} -> {b} missing from the graph'


def test_no_op_is_always_legal():
    for state in InquiryStatus:
        assert sm.can_transition(state, state)


def test_can_transition_rejects_an_undeclared_jump():
    assert not sm.can_transition(InquiryStatus.READY, InquiryStatus.BOOKED)
    assert not sm.can_transition(InquiryStatus.SENT, InquiryStatus.READY)


def test_assert_transition_names_both_states():
    with pytest.raises(sm.IllegalTransition) as exc:
        sm.assert_transition(InquiryStatus.BOOKED, InquiryStatus.READY, context='unit')
    assert 'booked' in str(exc.value) and 'ready' in str(exc.value)
    assert 'unit' in str(exc.value)


# --------------------------------------------------------------------------- #
# 2. advance_status semantics
# --------------------------------------------------------------------------- #

def test_advance_status_is_idempotent(db):
    q = Inquiry(id='i', tenant_id='tenant-demo', raw_message='x', missing_fields=[],
                status='ready')
    db.add(q)
    db.commit()

    assert sm.advance_status(q, InquiryStatus.SCHEDULED) is True
    assert q.status == 'scheduled'
    # Retrying the same stage must be a quiet no-op, not an error: webhooks redeliver.
    assert sm.advance_status(q, InquiryStatus.SCHEDULED) is False
    assert q.status == 'scheduled'


def test_advance_status_refuses_a_rewind(db):
    q = Inquiry(id='i', tenant_id='tenant-demo', raw_message='x', missing_fields=[],
                status='booked')
    db.add(q)
    db.commit()
    with pytest.raises(sm.IllegalTransition):
        sm.advance_status(q, InquiryStatus.READY)
    assert q.status == 'booked', 'a refused transition must not have mutated the row'


# --------------------------------------------------------------------------- #
# 3. The reproduced defect: editing an inquiry must not rewind its status
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_patch_does_not_rewind_an_in_flight_inquiry(db):
    """Regression: PATCHing a `scheduled` inquiry used to reset it to `ready`."""
    db.add(Inquiry(id='inq_1', tenant_id='tenant-demo', raw_message='x', missing_fields=[],
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ',
                   quantity=1, etd='2026-09-25', status='scheduled'))
    db.commit()

    q = patch_inquiry(db, 'tenant-demo', 'u', 'inq_1', InquiryPatch(commodity='furniture'))
    assert q.status == 'scheduled', 'a field edit must not move the pipeline backwards'
    assert q.commodity == 'furniture', 'but the edit itself must still be applied'


@pytest.mark.asyncio
async def test_patch_refuses_a_booked_inquiry(db):
    """Regression: PATCHing a `booked` inquiry used to reset it to `ready`.

    Reproduced against the running API before the guard existed. Once a booking exists the
    inquiry is part of what was agreed with the customer, so content is frozen.
    """
    db.add(Inquiry(id='inq_2', tenant_id='tenant-demo', raw_message='x', missing_fields=[],
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ',
                   quantity=1, etd='2026-09-25', status='booked'))
    db.commit()

    with pytest.raises(sm.InquiryLocked):
        patch_inquiry(db, 'tenant-demo', 'u', 'inq_2', InquiryPatch(destination='Oakland'))

    q = db.query(Inquiry).filter_by(id='inq_2').one()
    assert q.status == 'booked'
    assert q.destination == 'Los Angeles', 'the refused edit must not have been written'


@pytest.mark.asyncio
async def test_patch_refuses_a_closed_inquiry(db):
    db.add(Inquiry(id='inq_3', tenant_id='tenant-demo', raw_message='x', missing_fields=[],
                   status='closed'))
    db.commit()
    with pytest.raises(sm.InquiryLocked):
        patch_inquiry(db, 'tenant-demo', 'u', 'inq_3', InquiryPatch(commodity='x'))


@pytest.mark.asyncio
async def test_patch_still_rederives_status_while_being_specified(db):
    """The guard must not freeze the one phase where re-derivation is correct."""
    db.add(Inquiry(id='inq_4', tenant_id='tenant-demo', raw_message='x', missing_fields=['etd'],
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ',
                   quantity=1, status='needs_clarification'))
    db.commit()

    q = patch_inquiry(db, 'tenant-demo', 'u', 'inq_4', InquiryPatch(etd='2026-09-25'))
    assert q.status == 'ready' and q.missing_fields == []

    # ...and blanking a required field sends it back to clarification.
    # Note: an empty string, not None. `InquiryPatch` uses None to mean "field not supplied"
    # (`model_dump(exclude_none=True)`), so None cannot express "clear this field" -- a real
    # gap in the PATCH contract, recorded in docs/ARCHITECTURE_REVIEW.md rather than papered
    # over here.
    q = patch_inquiry(db, 'tenant-demo', 'u', 'inq_4', InquiryPatch(origin=''))
    assert q.status == 'needs_clarification'
    assert q.missing_fields == ['origin']


@pytest.mark.asyncio
async def test_patch_records_what_changed_in_the_audit_trail(db):
    db.add(Inquiry(id='inq_5', tenant_id='tenant-demo', raw_message='x', missing_fields=[],
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ',
                   quantity=1, etd='2026-09-25', status='ready'))
    db.commit()
    patch_inquiry(db, 'tenant-demo', 'u', 'inq_5', InquiryPatch(destination='Oakland'))

    from app.models import AuditEvent
    ev = (db.query(AuditEvent)
          .filter_by(tenant_id='tenant-demo', event_type='inquiry.updated', object_id='inq_5')
          .one())
    assert ev.payload['changed_fields'] == ['destination']
    assert ev.payload['status'] == 'ready'


# --------------------------------------------------------------------------- #
# 4. The orchestrator's real path
# --------------------------------------------------------------------------- #

@pytest.fixture
def recorded_transitions(monkeypatch):
    """Record every status change the service layer actually performs."""
    seen: list[tuple[str, str]] = []
    real = sm.advance_status

    def spy(row, target, *, context=''):
        before = str(row.status)
        changed = real(row, target, context=context)
        if changed:
            seen.append((before, str(target)))
        return changed

    for name in _ADVANCING_MODULES:
        monkeypatch.setattr(importlib.import_module(name), 'advance_status', spy)
    return seen


async def _complete_inquiry(db, status='ready'):
    db.add(Customer(id='cus_sm', tenant_id='tenant-demo', company='Acme',
                    email='a@acme-import.com'))
    db.add(Inquiry(id='inq_sm', tenant_id='tenant-demo', customer_id='cus_sm', raw_message='x',
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ', quantity=1,
                   etd='2026-09-25', missing_fields=[], status=status,
                   external_message_id='sm1'))
    db.commit()


@pytest.mark.asyncio
async def test_full_pipeline_transitions_exactly_match_the_happy_path(db, recorded_transitions):
    """The orchestrator's actual status sequence == the declared happy path.

    If someone reorders a stage or adds one that writes status out of turn, this fails and
    names the offending edge -- rather than the failure surfacing later as a corrupted
    inquiry nobody can explain.
    """
    await _complete_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_sm',
                                  auto_approve=True, vessel_name='MAEU-VESSEL')
    assert run.status == 'completed'

    expected = [(a.value, b.value) for a, b in zip(sm.HAPPY_PATH, sm.HAPPY_PATH[1:])]
    assert recorded_transitions == expected


@pytest.mark.asyncio
async def test_pipeline_stopped_at_the_gate_has_not_advanced_past_it(db, recorded_transitions):
    """Stopping for review must leave the inquiry at `waiting_approval`, never further."""
    await _complete_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_sm')
    assert run.status == 'waiting_approval'

    assert db.query(Inquiry).filter_by(id='inq_sm').one().status == 'waiting_approval'
    assert ('approved', 'sent') not in recorded_transitions
    assert ('sent', 'booked') not in recorded_transitions


@pytest.mark.asyncio
async def test_resume_path_is_also_legal(db, recorded_transitions):
    """The resume path re-enters the same code, so it must produce the same legal edges."""
    await _complete_inquiry(db)
    run = await orch.run_pipeline(db, 'tenant-demo', 'u', 't', 'inq_sm')
    quote_id = run.pending['quote_id']

    from app.services.approval import review_quote
    review_quote(db, 'tenant-demo', 'reviewer', quote_id, 'approve', 'ok')
    recorded_transitions.clear()

    resumed = await orch.resume_after_approval(db, 'tenant-demo', 'reviewer', 't', quote_id)
    assert resumed.status == 'completed'
    assert recorded_transitions == [('approved', 'sent'), ('sent', 'booked')]
