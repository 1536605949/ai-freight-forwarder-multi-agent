"""Harness: the governance layer between an agent and a deterministic business service.

The orchestrator decides *what* should happen; the harness decides *whether* it is allowed to.
Keeping those separate is what makes the graph safe to change: a new edge that reaches a
committing node still cannot commit without passing the same gate, because the gate is not in
the edge.

Import direction: this package depends only on `app.services.common.audit`. It does not
import `app.agents` (agent identity arrives as a string) and it does not import any business
service (the caller supplies the callable). That keeps it a leaf, so it cannot become part of
a cycle with the graph that uses it.
"""
from app.harness.actions import ACTIONS, ActionSpec, action_names, get_action
from app.harness.guard import (
    ALLOW, DENY, NEEDS_APPROVAL, ORCHESTRATOR,
    ApprovalRequired, Decision, HarnessDenied, HarnessError,
    check, decide, policy_table, run,
)
from app.harness.risk import Risk

__all__ = [
    'ACTIONS', 'ALLOW', 'ActionSpec', 'ApprovalRequired', 'DENY', 'Decision',
    'HarnessDenied', 'HarnessError', 'NEEDS_APPROVAL', 'ORCHESTRATOR', 'Risk',
    'action_names', 'check', 'decide', 'get_action', 'policy_table', 'run',
]
