"""Explicit state machine for `InquiryStatus`.

Why this module exists
----------------------
`app/enums.py` is a *vocabulary* — it lists which states are legal. It deliberately says
nothing about which state may follow which. That knowledge used to live implicitly, spread
across eight service functions that each did `inquiry.status = ...` and trusted the caller
to be standing in the right place.

Implicit transitions are how a `booked` inquiry gets rewound to `ready` by a routine field
edit — a defect that was reproduced against the running API before this module was written.
Making the graph explicit buys three concrete things:

1. Services can call `advance_status()` and get an error instead of silently corrupting the
   state history when they are invoked out of order.
2. The orchestrator's real path can be replayed against the graph in a test, so a future
   stage reorder fails loudly rather than producing an impossible history.
3. A reviewer reads one table instead of eight services.

The `advance_status()` contract
-------------------------------
Idempotent and forward-only. Calling it with the state the object is already in is a no-op
(safe to retry, which matters for webhook and worker redelivery). Calling it with a state
the graph does not permit raises. It never rewinds — a rewind is always a bug or an
unmodelled recovery feature, and both deserve to be loud.
"""
from __future__ import annotations

from typing import Protocol

from app.enums import InquiryStatus


class _Statused(Protocol):
    """Anything carrying a status column (ORM rows satisfy this structurally)."""

    status: str


class IllegalTransition(ValueError):
    """Raised when code attempts a status change the business graph does not allow."""

    def __init__(self, current: str, target: str, *, context: str = '') -> None:
        self.current = str(current)
        self.target = str(target)
        suffix = f' ({context})' if context else ''
        super().__init__(f'illegal_status_transition: {self.current} -> {self.target}{suffix}')


class InquiryLocked(ValueError):
    """Raised when content is edited on an inquiry whose commitment is already recorded."""

    def __init__(self, current: str) -> None:
        self.current = str(current)
        super().__init__(f'inquiry_locked: status {self.current} is immutable')


# --------------------------------------------------------------------------- #
# Which states are still "being specified"
# --------------------------------------------------------------------------- #

# Only in these states may a plain field edit re-derive the status from field completeness.
# Everywhere else the status is owned by the pipeline, and a field edit must not move it.
CONTENT_EDITABLE: frozenset[InquiryStatus] = frozenset({
    InquiryStatus.NEEDS_CLARIFICATION,
    InquiryStatus.READY,
})

# A booking exists, or the record is closed. Editing content here would falsify what was
# actually agreed and sent to the customer, so it is refused outright.
LOCKED: frozenset[InquiryStatus] = frozenset({
    InquiryStatus.BOOKED,
    InquiryStatus.CLOSED,
})


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #
# Every edge below is one the running code performs, plus the CLOSED sink that any
# non-terminal state may enter administratively.
#
# Note what is deliberately ABSENT: there is no edge from a later state back to an earlier
# one. Recovery ("the customer changed the destination after we quoted") is a real business
# need, but it is a distinct, audited operation — not something a re-run of a search
# endpoint should do as a side effect. When that feature is built it adds its own named
# edge here, on purpose.
INQUIRY_TRANSITIONS: dict[InquiryStatus, frozenset[InquiryStatus]] = {
    InquiryStatus.NEEDS_CLARIFICATION: frozenset({InquiryStatus.READY, InquiryStatus.CLOSED}),
    InquiryStatus.READY: frozenset({InquiryStatus.SCHEDULED, InquiryStatus.NEEDS_CLARIFICATION,
                                    InquiryStatus.CLOSED}),
    InquiryStatus.SCHEDULED: frozenset({InquiryStatus.WAITING_SUPPLIER_QUOTES, InquiryStatus.CLOSED}),
    InquiryStatus.WAITING_SUPPLIER_QUOTES: frozenset({InquiryStatus.QUOTE_READY,
                                                     InquiryStatus.WAITING_APPROVAL,
                                                     InquiryStatus.CLOSED}),
    InquiryStatus.QUOTE_READY: frozenset({InquiryStatus.WAITING_APPROVAL, InquiryStatus.CLOSED}),
    InquiryStatus.WAITING_APPROVAL: frozenset({InquiryStatus.APPROVED, InquiryStatus.REJECTED,
                                               InquiryStatus.CLOSED}),
    InquiryStatus.APPROVED: frozenset({InquiryStatus.SENT, InquiryStatus.CLOSED}),
    InquiryStatus.REJECTED: frozenset({InquiryStatus.CLOSED}),
    InquiryStatus.SENT: frozenset({InquiryStatus.BOOKED, InquiryStatus.CLOSED}),
    InquiryStatus.BOOKED: frozenset({InquiryStatus.CLOSED}),
    InquiryStatus.CLOSED: frozenset(),
}

# The happy path, spelled out for tests and docs. A pipeline run must follow it exactly.
HAPPY_PATH: tuple[InquiryStatus, ...] = (
    InquiryStatus.READY,
    InquiryStatus.SCHEDULED,
    InquiryStatus.WAITING_SUPPLIER_QUOTES,
    InquiryStatus.WAITING_APPROVAL,
    InquiryStatus.APPROVED,
    InquiryStatus.SENT,
    InquiryStatus.BOOKED,
)


def _coerce(value: InquiryStatus | str) -> InquiryStatus:
    return value if isinstance(value, InquiryStatus) else InquiryStatus(str(value))


def can_transition(current: InquiryStatus | str, target: InquiryStatus | str) -> bool:
    """Is `current -> target` a legal edge? A no-op (current == target) counts as legal."""
    cur, tgt = _coerce(current), _coerce(target)
    if cur is tgt:
        return True
    return tgt in INQUIRY_TRANSITIONS.get(cur, frozenset())


def assert_transition(current: InquiryStatus | str, target: InquiryStatus | str, *,
                      context: str = '') -> None:
    """Raise `IllegalTransition` unless `current -> target` is legal."""
    if not can_transition(current, target):
        raise IllegalTransition(str(_coerce(current)), str(_coerce(target)), context=context)


def advance_status(row: _Statused, target: InquiryStatus, *, context: str = '') -> bool:
    """Move `row.status` to `target` if the graph allows it.

    Returns True when the status actually changed, False when it was already `target`.

    Idempotent by design: retrying a stage that already succeeded is a no-op rather than an
    error, because webhooks and the worker redeliver. Advancing *backwards* is an error,
    because no legitimate caller needs it and every occurrence so far has been a bug.
    """
    current = _coerce(row.status)
    if current is target:
        return False
    assert_transition(current, target, context=context)
    row.status = target
    return True


def derived_inquiry_status(missing: list[str]) -> InquiryStatus:
    """The status implied by field completeness, for use while the inquiry is still being
    specified. Never apply this to an inquiry past `CONTENT_EDITABLE` — see `patch_inquiry`."""
    return InquiryStatus.NEEDS_CLARIFICATION if missing else InquiryStatus.READY


__all__ = [
    'CONTENT_EDITABLE', 'LOCKED', 'INQUIRY_TRANSITIONS', 'HAPPY_PATH',
    'IllegalTransition', 'InquiryLocked',
    'can_transition', 'assert_transition', 'advance_status', 'derived_inquiry_status',
]
