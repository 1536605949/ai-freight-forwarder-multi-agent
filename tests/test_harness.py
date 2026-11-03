"""The governance layer: risk grading, entitlement, roles, approval, and fail-closed.

These tests are written against the *decision*, not against a happy path, because the value
of this module is the refusal. A test suite that only proves allowed calls still work would
pass just as happily if every check were deleted.

The three assertions that matter most:

* an undeclared action is refused rather than defaulted to allowed;
* an approval cannot repair a broken precondition (wrong agent, missing role);
* a refused action's callable is never invoked, and the refusal is still on disk afterwards.
"""
from __future__ import annotations

import pytest

from app.agents.registry import AGENTS
from app.harness import (
    ALLOW, DENY, NEEDS_APPROVAL, ORCHESTRATOR,
    ApprovalRequired, HarnessDenied, Risk,
    check, decide, policy_table, run,
)
from app.harness.actions import ACTIONS, ActionSpec
from app.models import AuditEvent


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #

def test_an_undeclared_action_is_denied_not_allowed():
    """The dangerous default is `allow`. This is the test that keeps it from being that."""
    decision = decide('quote.refund_everything', agent='quoting', approved=True)

    assert decision.outcome == DENY
    assert decision.reason == 'unknown_action'
    assert decision.risk is None, 'an unknown action has no risk, and must not borrow one'


def test_an_agent_cannot_act_outside_its_own_actions():
    decision = decide('quote.send', agent='acquisition', approved=True)

    assert decision.outcome == DENY
    assert decision.reason == 'agent_not_entitled'
    # The risk is reported even on a refusal: an auditor wants to know what was attempted.
    assert decision.risk == Risk.EXTERNAL


def test_the_orchestrator_may_route_but_may_not_commit():
    """Routing is not authority. The orchestrator gets READ and nothing above it."""
    assert decide('vessel.position.refresh', agent=ORCHESTRATOR).outcome == ALLOW

    for action in ('supplier.rfq.send', 'quote.price', 'quote.send', 'booking.create'):
        decision = decide(action, agent=ORCHESTRATOR, approved=True, roles=('reviewer',))
        assert decision.outcome == DENY, f'{action} must not be reachable by the router'
        assert decision.reason == 'agent_not_entitled'


# --------------------------------------------------------------------------- #
# Preconditions, in order
# --------------------------------------------------------------------------- #

def test_a_missing_role_is_reported_as_a_role_problem_not_as_an_approval_problem():
    """Order matters operationally.

    An operator told `approval_required` goes and finds an approver. If the real problem was
    that the caller is not a reviewer, they come back refused again with no new information.
    """
    decision = decide('prospect.outreach.send', agent='acquisition',
                      roles=('sales',), approved=False)

    assert decision.outcome == DENY
    assert decision.reason.startswith('role_missing:')
    assert 'reviewer' in decision.reason


def test_approval_cannot_repair_a_missing_role():
    decision = decide('prospect.outreach.send', agent='acquisition',
                      roles=('sales',), approved=True)

    assert decision.outcome == DENY, 'approval is not a substitute for the right role'
    assert decision.reason.startswith('role_missing:')


def test_approval_cannot_repair_a_wrong_agent():
    decision = decide('quote.send', agent='tracking', approved=True, roles=('reviewer',))

    assert decision.outcome == DENY
    assert decision.reason == 'agent_not_entitled'


def test_an_unapproved_external_action_is_not_allowed():
    decision = decide('quote.send', agent='quoting', approved=False)

    assert decision.outcome == NEEDS_APPROVAL
    assert decision.outcome != ALLOW
    assert decision.reason == 'approval_required'


def test_a_draft_write_needs_no_approval():
    """Drafting harms nobody; sending does. If this fails, every draft becomes a ticket."""
    assert decide('quote.price', agent='quoting').outcome == ALLOW
    assert decide('supplier.quote.record', agent='sourcing').outcome == ALLOW


# --------------------------------------------------------------------------- #
# The worst level is not configurable
# --------------------------------------------------------------------------- #

def test_a_commitment_action_cannot_be_declared_approval_free():
    """A future `COMMITMENT` action cannot opt out by leaving the flag at its default."""
    spec = ActionSpec('booking.cancel', 'quoting', Risk.COMMITMENT, 'cancel a booking',
                      requires_approval=False)

    assert spec.approval_required is True
    assert decide('booking.create', agent='quoting', approved=False).outcome == NEEDS_APPROVAL


def test_every_declared_action_is_owned_by_a_known_agent():
    unknown = [spec.name for spec in ACTIONS.values() if spec.agent not in AGENTS]
    assert not unknown, f'actions owned by a non-existent agent: {unknown}'


def test_the_policy_table_is_what_the_endpoint_reports():
    table = policy_table()
    assert {row['name'] for row in table} == set(ACTIONS)
    assert all('risk_label' in row and 'requires_approval' in row for row in table)


# --------------------------------------------------------------------------- #
# Execution: refused actions never run, and leave a trace
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_refused_action_never_reaches_the_service(db):
    called = []

    async def service_call():
        called.append(True)
        return 'sent'

    with pytest.raises(ApprovalRequired):
        await run('quote.send', agent='quoting', db=db, tenant_id='tenant-demo', actor='u',
                  approved=False, call=service_call)

    assert called == [], 'the refusal must happen before the service is called'
    db.rollback()


@pytest.mark.asyncio
async def test_a_refusal_survives_the_rollback_that_follows_it(db):
    """The caller raises, the graph rolls back -- and the evidence must still be there.

    An uncommitted audit row would disappear exactly when it is most needed: a blocked
    attempt is the thing an auditor is looking for.
    """
    with pytest.raises(HarnessDenied):
        await run('booking.create', agent='tracking', db=db, tenant_id='tenant-demo',
                  actor='u', call=lambda: None)

    db.rollback()
    rows = db.query(AuditEvent).filter_by(event_type='harness.deny').all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload['action'] == 'booking.create'
    assert payload['agent'] == 'tracking'
    assert payload['reason'] == 'agent_not_entitled'
    assert payload['risk_label'] == '不可逆承诺'


@pytest.mark.asyncio
async def test_an_allowed_action_runs_and_is_recorded(db):
    async def service_call():
        return 'ok'

    result = await run('quote.price', agent='quoting', db=db, tenant_id='tenant-demo',
                       actor='u', call=service_call)

    assert result == 'ok'
    rows = db.query(AuditEvent).filter_by(event_type='harness.allow').all()
    assert len(rows) == 1 and rows[0].payload['action'] == 'quote.price'


def test_check_raises_for_the_two_refusal_shapes(db):
    with pytest.raises(HarnessDenied):
        check('nope', agent='quoting', db=db, tenant_id='tenant-demo', actor='u')
    with pytest.raises(ApprovalRequired):
        check('quote.send', agent='quoting', db=db, tenant_id='tenant-demo', actor='u')
    db.rollback()
