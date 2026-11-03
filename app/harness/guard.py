"""The single decision point between an agent and a side effect.

Every refusal here is *fail-closed*: an action that is not declared is refused, an agent that
does not own the action is refused, a missing role is refused, and an unapproved `EXTERNAL`
action is refused. There is no branch that returns `allow` because something could not be
determined -- that branch is `deny`, always.

Order of the checks is part of the contract
-------------------------------------------
1. unknown action -> deny. A typo must not become a permission.
2. agent does not own the action -> deny. Identity is not negotiable.
3. role missing -> deny. `approval` cannot fix "you are the wrong person".
4. approval missing -> needs_approval (not allow).

Each step is a precondition of the next, so the reason that comes back is the *first* rule
broken rather than the last one checked. That matters operationally: an operator told
"approval_required" will go find an approver, and if the real problem was that the caller was
the wrong agent, they will come back refused again with no idea why.

Why refusals are `ValueError`
-----------------------------
`app/graph/builder.py` already treats a `ValueError` from a node as a business result rather
than a crash: the run ends in a `failed` status whose `stop_reason` names the node and the
reason. A governance refusal is exactly that -- a business result. Subclassing `ValueError`
means the graph does not need a second, parallel error path for governance, and a refusal
shows up in the trace with the same shape as "insufficient supplier quotes".
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session

from app.harness.actions import ACTIONS
from app.harness.risk import Risk
from app.services.common import audit

ALLOW = 'allow'
DENY = 'deny'
NEEDS_APPROVAL = 'needs_approval'

# The only identity that may act outside its own ownership, and only for `READ`. Routing is
# not authority: an orchestrator can ask an agent to look something up, never to commit.
ORCHESTRATOR = 'orchestrator'


@dataclass(frozen=True)
class Decision:
    outcome: str
    action: str
    agent: str
    risk: Risk | None
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def as_dict(self) -> dict:
        return {
            'outcome': self.outcome,
            'action': self.action,
            'agent': self.agent,
            'risk': None if self.risk is None else int(self.risk),
            'risk_label': 'unknown' if self.risk is None else self.risk.label,
            'reason': self.reason,
        }


class HarnessError(ValueError):
    """A governance refusal, shaped as a business result so the graph can record it."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(f'{decision.outcome}: {decision.reason}')
        self.decision = decision


class HarnessDenied(HarnessError):
    """Refused before execution. The action never ran."""


class ApprovalRequired(HarnessError):
    """Refused because a human approval was not recorded. The action never ran."""


def decide(
    action: str,
    *,
    agent: str,
    roles: Sequence[str] = (),
    approved: bool = False,
) -> Decision:
    """Pure decision. No database, no audit, no side effects -- so it is cheap to test."""
    spec = ACTIONS.get(action)
    if spec is None:
        return Decision(DENY, action, agent, None, 'unknown_action')

    entitled = spec.agent == agent or (agent == ORCHESTRATOR and spec.risk <= Risk.READ)
    if not entitled:
        return Decision(DENY, action, agent, spec.risk, 'agent_not_entitled')

    held = set(roles or ())
    missing = [role for role in spec.roles if role not in held]
    if missing:
        return Decision(DENY, action, agent, spec.risk, f'role_missing:{",".join(missing)}')

    if spec.approval_required and not approved:
        return Decision(NEEDS_APPROVAL, action, agent, spec.risk, 'approval_required')

    return Decision(ALLOW, action, agent, spec.risk, 'ok')


def check(
    action: str,
    *,
    agent: str,
    db: Session | None = None,
    tenant_id: str = '',
    actor: str = '',
    roles: Sequence[str] = (),
    approved: bool = False,
    detail: dict | None = None,
) -> Decision:
    """Decide, record, and raise on anything that is not `allow`."""
    decision = decide(action, agent=agent, roles=roles, approved=approved)
    if db is not None:
        _record(db, tenant_id, actor, decision, detail)

    if decision.outcome == DENY:
        raise HarnessDenied(decision)
    if decision.outcome == NEEDS_APPROVAL:
        raise ApprovalRequired(decision)
    return decision


async def run(
    action: str,
    *,
    agent: str,
    db: Session | None = None,
    tenant_id: str = '',
    actor: str = '',
    roles: Sequence[str] = (),
    approved: bool = False,
    detail: dict | None = None,
    call: Callable[[], Any],
) -> Any:
    """`check` the action, then perform it. The callable is never reached when refused."""
    check(action, agent=agent, db=db, tenant_id=tenant_id, actor=actor, roles=roles,
          approved=approved, detail=detail)
    result = call()
    if inspect.isawaitable(result):
        result = await result
    return result


def _record(db: Session, tenant_id: str, actor: str, decision: Decision,
            detail: dict | None) -> None:
    audit(db, tenant_id, actor, f'harness.{decision.outcome}', 'action', decision.action,
          {**decision.as_dict(), **(detail or {})})
    if decision.outcome != ALLOW:
        # A refusal has to outlive the rollback that usually follows it. The caller raises,
        # the graph turns that into a `failed` node, and an uncommitted audit row would
        # disappear with the transaction -- leaving no evidence that anything was attempted.
        # Nothing else has been written at this point (the action did not run), so this
        # commit cannot persist half a business change.
        db.commit()


def policy_table() -> list[dict]:
    """The declared surface, for docs, tests and the admin endpoint."""
    return [spec.as_dict() for spec in ACTIONS.values()]


__all__ = [
    'ALLOW', 'DENY', 'NEEDS_APPROVAL', 'ORCHESTRATOR',
    'ApprovalRequired', 'Decision', 'HarnessDenied', 'HarnessError',
    'check', 'decide', 'policy_table', 'run',
]
