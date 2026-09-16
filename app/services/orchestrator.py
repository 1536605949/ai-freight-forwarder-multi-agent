"""Unified orchestration entry — the "one button" that runs the whole business flow.

Why this exists
---------------
Every stage of the workflow already had its own service function and HTTP route, but a
demonstration (or a bid evaluator) had to call eight endpoints in the right order and know
which id fed which. That is fine for an integrator and fatal for a demo.

This module chains the stages and returns a *stage-by-stage trace*, so the front-end can
render progress without knowing the business logic.

The rule that shapes everything here
------------------------------------
**Orchestration sequences work; it does not grant permission.**

Automation is allowed to *do more work*, never to *bypass a gate*. Concretely: this module
will chain "parse → schedules → rates → RFQ → collect quotes → optimize" because those are
internal, reversible, no-outbound-commitment steps. It stops dead at the first step that
would contact the customer or commit money (quote send, booking), and returns
`waiting_approval`. Resuming is the caller's explicit decision — never a side effect of
retrying.

This is why `run_pipeline` accepts a `start_stage`: a resumed run re-enters the same code
path rather than having a second, subtly different "continue" implementation that can drift
out of sync with the first.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import CustomerQuote, Inquiry, RFQ, SupplierQuote
from app.services import approval as approval_svc
from app.services import booking as booking_svc
from app.services import pricing as pricing_svc
from app.services import rates as rates_svc
from app.services import rfq as rfq_svc
from app.services import schedule as schedule_svc
from app.services import vessel as vessel_svc
from app.services.common import audit, uid

# The canonical stage order. The demo page renders these in sequence, so the names are
# part of the UI contract: renaming one is a breaking change for the front-end.
STAGES: list[str] = [
    'parse',
    'schedules',
    'rates',
    'rfq',
    'supplier_quotes',
    'optimize',
    'hitl_review',
    'quote_send',
    'booking',
    'vessel_tracking',
]

# Stages where automation MUST stop and a human must decide.
GATED_STAGES: set[str] = {'hitl_review', 'quote_send'}

# Stages that are read-only / reversible and therefore safe to auto-run.
AUTO_STAGES: set[str] = {'parse', 'schedules', 'rates', 'rfq', 'supplier_quotes', 'optimize'}


@dataclass
class StageResult:
    """One entry in the execution trace."""
    stage: str
    status: str                       # ok | waiting_approval | skipped | failed
    detail: str = ''
    outputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {'stage': self.stage, 'status': self.status, 'detail': self.detail, 'outputs': self.outputs}


@dataclass
class PipelineRun:
    run_id: str
    inquiry_id: str | None
    trace: list[StageResult]
    status: str                       # running | waiting_approval | completed | failed
    pending: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            'run_id': self.run_id,
            'inquiry_id': self.inquiry_id,
            'status': self.status,
            'pending': self.pending,
            'trace': [s.as_dict() for s in self.trace],
        }


def _ok(stage: str, detail: str = '', **outputs) -> StageResult:
    return StageResult(stage, 'ok', detail, outputs)


class _Abort(Exception):
    """Internal control flow: stop the pipeline and return what we have so far."""

    def __init__(self, stage: str, status: str, detail: str, **outputs) -> None:
        super().__init__(detail)
        self.result = StageResult(stage, status, detail, outputs)


async def run_pipeline(
    db: Session,
    tenant_id: str,
    actor: str,
    trace_id: str,
    inquiry_id: str,
    *,
    start_stage: str = 'parse',
    auto_supplier_quotes: bool = True,
    vessel_name: str | None = None,
    auto_approve: bool = False,
) -> PipelineRun:
    """Run the workflow from `start_stage` until a gate or the end.

    Args:
        auto_supplier_quotes: seed synthetic supplier quotes so the pricing comparison has
            something to compare. Demo/test convenience — a real deployment receives these
            from the supplier mailbox webhook instead.
        auto_approve: skip the human gate. **Only legitimate in a demo/test**, never in
            production. Defaults to False, and the caller must opt in explicitly.
    """
    run_id = uid('run_')
    trace: list[StageResult] = []
    started = STAGES.index(start_stage) if start_stage in STAGES else 0

    audit(db, tenant_id, actor, 'pipeline.started', 'inquiry', inquiry_id,
          {'run_id': run_id, 'start_stage': start_stage})

    inquiry = db.query(Inquiry).filter_by(id=inquiry_id, tenant_id=tenant_id).first()
    if not inquiry:
        raise KeyError('inquiry_not_found')

    run = PipelineRun(run_id=run_id, inquiry_id=inquiry_id, trace=trace, status='running')

    for stage in STAGES[started:]:
        try:
            await _run_stage(db, tenant_id, actor, trace_id, inquiry, stage, trace,
                             auto_supplier_quotes=auto_supplier_quotes,
                             vessel_name=vessel_name, auto_approve=auto_approve)
        except _Abort as stop:
            trace.append(stop.result)
            run.status = stop.result.status
            run.pending = stop.result.outputs
            break
        except (KeyError, ValueError) as exc:
            # A business-rule failure is a *result*, not a crash. Record it and stop, so the
            # caller sees exactly which stage refused and why.
            trace.append(StageResult(stage, 'failed', str(exc)))
            run.status = 'failed'
            run.pending = {'stage': stage, 'reason': str(exc)}
            break
    else:
        run.status = 'completed'

    audit(db, tenant_id, actor, 'pipeline.finished', 'inquiry', inquiry_id,
          {'run_id': run_id, 'status': run.status,
           'stages_ok': [s.stage for s in trace if s.status == 'ok']})
    db.commit()
    return run


async def _run_stage(db, tenant_id, actor, trace_id, inquiry, stage, trace, *,
                     auto_supplier_quotes, vessel_name, auto_approve) -> None:
    """Execute exactly one stage. Raises `_Abort` when the pipeline must stop.

    Stages are mixed sync/async (parsing and seeding are pure DB work; schedules, rates,
    RFQ and tracking call providers). Awaiting conditionally here keeps every handler's
    signature uniform instead of forcing needless `async` on the pure-DB ones.
    """
    handler = _HANDLERS[stage]
    result = handler(db, tenant_id, actor, trace_id, inquiry,
                     auto_supplier_quotes=auto_supplier_quotes,
                     vessel_name=vessel_name, auto_approve=auto_approve)
    if inspect.isawaitable(result):
        result = await result
    if result is not None:
        trace.append(result)

# --------------------------------------------------------------------------- #
# Stage handlers
# --------------------------------------------------------------------------- #

def _stage_parse(db, tenant_id, actor, trace_id, inquiry, **kw) -> StageResult:
    """Validate the inquiry is complete enough to price. No-op if already parsed."""
    if inquiry.missing_fields:
        raise _Abort('parse', 'waiting_approval', 'inquiry_incomplete',
                     inquiry_id=inquiry.id, missing_fields=inquiry.missing_fields)
    return _ok('parse', 'inquiry complete', inquiry_id=inquiry.id,
               origin=inquiry.origin, destination=inquiry.destination,
               equipment=inquiry.equipment, etd=inquiry.etd)


async def _stage_schedules(db, tenant_id, actor, trace_id, inquiry, **kw) -> StageResult:
    rows = await schedule_svc.search_schedules(db, tenant_id, actor, inquiry.id)
    first = rows[0] if rows else None
    return _ok('schedules', f'{len(rows)} sailings',
               schedule_count=len(rows),
               vessel=first.vessel if first else None,
               voyage=first.voyage if first else None,
               etd=first.etd if first else None,
               eta=first.eta if first else None)


async def _stage_rates(db, tenant_id, actor, trace_id, inquiry, **kw) -> StageResult:
    rows = await rates_svc.search_rates(db, tenant_id, actor, inquiry.id)
    cheapest = min(rows, key=lambda x: x.total_cost) if rows else None
    return _ok('rates', f'{len(rows)} rates',
               rate_count=len(rows),
               rate_ids=[x.id for x in rows],
               cheapest_carrier=cheapest.carrier if cheapest else None,
               cheapest_total=cheapest.total_cost if cheapest else None,
               sources=sorted({x.source for x in rows}))


async def _stage_rfq(db, tenant_id, actor, trace_id, inquiry, **kw) -> StageResult:
    """Open an RFQ unless one is already open/received (resume-safe)."""
    existing = (db.query(RFQ)
                .filter_by(tenant_id=tenant_id, inquiry_id=inquiry.id)
                .order_by(RFQ.created_at.desc()).first())
    if existing:
        return _ok('rfq', 'reusing existing rfq', rfq_id=existing.id,
                   status=existing.status, expected=existing.expected_suppliers,
                   received=existing.received_quotes)
    rfq, results = await rfq_svc.create_rfq(db, tenant_id, actor, trace_id, inquiry.id)
    return _ok('rfq', f'sent to {len(results)} suppliers', rfq_id=rfq.id,
               expected_suppliers=rfq.expected_suppliers)


def _stage_supplier_quotes(db, tenant_id, actor, trace_id, inquiry, **kw) -> StageResult:
    """Ensure enough supplier quotes exist for a meaningful comparison.

    In production these arrive asynchronously via the supplier-mailbox webhook; here the
    demo seeds deterministic synthetic quotes so the pipeline can continue in one call.
    """
    rfq = (db.query(RFQ).filter_by(tenant_id=tenant_id, inquiry_id=inquiry.id)
           .order_by(RFQ.created_at.desc()).first())
    if not rfq:
        raise _Abort('supplier_quotes', 'failed', 'rfq_missing')

    have = db.query(SupplierQuote).filter_by(tenant_id=tenant_id, rfq_id=rfq.id).count()
    seeded = 0
    if kw.get('auto_supplier_quotes') and have < 2:
        from app.schemas import SupplierQuoteIn
        # Deterministic, slightly-under-cutting quotes: supplier B beats A on price but
        # with worse transit, so the scoring function has a real trade-off to resolve.
        demo = [('Ocean Partner A', 'a@carrier.demo', 2150.0, 140.0, 14, 7, 0.86),
                ('Ocean Partner B', 'b@carrier.demo', 1980.0, 165.0, 18, 4, 0.91)]
        for name, addr, ocean, sur, transit, free, rel in demo[have:]:
            rfq_svc.add_supplier_quote(db, tenant_id, actor, rfq.id, SupplierQuoteIn(
                supplier_name=name, supplier_email=addr, ocean_freight=ocean,
                surcharges=sur, transit_days=transit, free_time_days=free,
                reliability_score=rel))
            seeded += 1

    total = db.query(SupplierQuote).filter_by(tenant_id=tenant_id, rfq_id=rfq.id).count()
    db.refresh(rfq)
    return _ok('supplier_quotes', f'{total} quotes' + (f' ({seeded} seeded)' if seeded else ''),
               rfq_id=rfq.id, quote_count=total, seeded=seeded)


async def _stage_optimize(db, tenant_id, actor, trace_id, inquiry, **kw) -> StageResult:
    rfq = (db.query(RFQ).filter_by(tenant_id=tenant_id, inquiry_id=inquiry.id)
           .order_by(RFQ.created_at.desc()).first())
    if not rfq:
        raise _Abort('optimize', 'failed', 'rfq_missing')
    cq = await pricing_svc.optimize_quote(db, tenant_id, actor, trace_id, rfq.id)
    return _ok('optimize', f'sell {cq.sell_amount}',
               quote_id=cq.id, cost_amount=cq.cost_amount,
               margin_amount=cq.margin_amount, sell_amount=cq.sell_amount,
               supplier_quote_id=cq.supplier_quote_id)


def _latest_quote(db, tenant_id, inquiry) -> CustomerQuote | None:
    return (db.query(CustomerQuote).filter_by(tenant_id=tenant_id, inquiry_id=inquiry.id)
            .order_by(CustomerQuote.created_at.desc()).first())


def _stage_hitl_review(db, tenant_id, actor, trace_id, inquiry, *, auto_approve, **kw) -> StageResult:
    """THE human gate. Automation stops here; a reviewer decides.

    `auto_approve` exists for demo/test only. It is deliberately a named, explicit flag
    rather than an environment variable, so it can never be enabled by accident in
    production config.
    """
    cq = _latest_quote(db, tenant_id, inquiry)
    if not cq:
        raise _Abort('hitl_review', 'failed', 'quote_missing')
    if cq.status == 'approved':
        return _ok('hitl_review', 'already approved', quote_id=cq.id, approved_by=cq.approved_by)
    if cq.status == 'rejected':
        raise _Abort('hitl_review', 'failed', 'quote_rejected', quote_id=cq.id)
    if not auto_approve:
        raise _Abort('hitl_review', 'waiting_approval', 'human_review_required',
                     quote_id=cq.id, sell_amount=cq.sell_amount, cost_amount=cq.cost_amount,
                     margin_amount=cq.margin_amount,
                     resume_hint=f'POST /api/v1/quotes/{cq.id}/review {{"action":"approve"}}')
    approval_svc.review_quote(db, tenant_id, actor, cq.id, 'approve', 'auto-approved (demo mode)')
    return _ok('hitl_review', 'approved (demo auto-approve)', quote_id=cq.id, auto_approved=True)


async def _stage_quote_send(db, tenant_id, actor, trace_id, inquiry, *, auto_approve, **kw) -> StageResult:
    cq = _latest_quote(db, tenant_id, inquiry)
    if not cq:
        raise _Abort('quote_send', 'failed', 'quote_missing')
    if cq.status == 'sent':
        return _ok('quote_send', 'already sent', quote_id=cq.id)
    if cq.status != 'approved':
        # This is the hard gate. It is enforced in the service layer and merely *surfaced*
        # here; orchestration cannot talk its way past it.
        raise _Abort('quote_send', 'waiting_approval', 'approval_required', quote_id=cq.id)
    q, result = await approval_svc.send_quote(db, tenant_id, actor, cq.id)
    return _ok('quote_send', 'sent to customer', quote_id=q.id,
               message_id=getattr(result, 'message_id', None))


async def _stage_booking(db, tenant_id, actor, trace_id, inquiry, **kw) -> StageResult:
    cq = _latest_quote(db, tenant_id, inquiry)
    if not cq or cq.status != 'sent':
        raise _Abort('booking', 'waiting_approval', 'customer_quote_must_be_sent_before_booking',
                     quote_id=cq.id if cq else None)
    b, idempotent = await booking_svc.create_booking(
        db, tenant_id, actor, inquiry.id, cq.id,
        {'equipment': inquiry.equipment, 'quantity': inquiry.quantity,
         'commodity': inquiry.commodity, 'weight_kg': inquiry.weight_kg},
    )
    return _ok('booking', 'booking confirmed', booking_id=b.id,
               provider_ref=b.provider_ref, status=b.status, idempotent=idempotent)


async def _stage_vessel_tracking(db, tenant_id, actor, trace_id, inquiry, *, vessel_name, **kw) -> StageResult:
    from app.models import Booking
    booking = (db.query(Booking).filter_by(tenant_id=tenant_id, inquiry_id=inquiry.id)
               .order_by(Booking.created_at.desc()).first())
    if not booking:
        raise _Abort('vessel_tracking', 'waiting_approval', 'booking_required')
    if not vessel_name:
        return _ok('vessel_tracking', 'skipped: no vessel specified', booking_id=booking.id)
    out = await vessel_svc.refresh_position(db, tenant_id, actor, vessel_name, None, booking.id)
    pos = out.get('position') or {}
    return _ok('vessel_tracking',
               'delay alert raised' if out.get('alert') else 'on schedule',
               booking_id=booking.id, delayed=out.get('delayed'),
               delay_hours=pos.get('delay_hours'), next_port=pos.get('next_port'),
               alert=out.get('alert'))


_HANDLERS = {
    'parse': _stage_parse,
    'schedules': _stage_schedules,
    'rates': _stage_rates,
    'rfq': _stage_rfq,
    'supplier_quotes': _stage_supplier_quotes,
    'optimize': _stage_optimize,
    'hitl_review': _stage_hitl_review,
    'quote_send': _stage_quote_send,
    'booking': _stage_booking,
    'vessel_tracking': _stage_vessel_tracking,
}


# --------------------------------------------------------------------------- #
# Resume helper
# --------------------------------------------------------------------------- #

async def resume_after_approval(
    db: Session, tenant_id: str, actor: str, trace_id: str, quote_id: str,
    *, vessel_name: str | None = None,
) -> PipelineRun:
    """Reviewer approved (or rejected) — continue the pipeline from the gate.

    Kept separate from `run_pipeline` only to make the *intent* obvious at the call site;
    it delegates to the same code path so the two can never diverge.
    """
    cq = db.query(CustomerQuote).filter_by(id=quote_id, tenant_id=tenant_id).first()
    if not cq:
        raise KeyError('quote_not_found')
    if cq.status == 'rejected':
        return PipelineRun(run_id=uid('run_'), inquiry_id=cq.inquiry_id, status='failed',
                           trace=[StageResult('hitl_review', 'failed', 'quote_rejected',
                                              {'quote_id': quote_id})])
    # Re-enter at the gate so the trace shows the reviewer's decision, then continue.
    return await run_pipeline(db, tenant_id, actor, trace_id, cq.inquiry_id,
                              start_stage='hitl_review', vessel_name=vessel_name)


def pipeline_status(db: Session, tenant_id: str, inquiry_id: str) -> dict[str, Any]:
    """A cheap, side-effect-free snapshot of where this inquiry currently stands.

    Used by the demo page to render state without re-running anything.
    """
    inquiry = db.query(Inquiry).filter_by(id=inquiry_id, tenant_id=tenant_id).first()
    if not inquiry:
        raise KeyError('inquiry_not_found')
    rfq = (db.query(RFQ).filter_by(tenant_id=tenant_id, inquiry_id=inquiry_id)
           .order_by(RFQ.created_at.desc()).first())
    cq = _latest_quote(db, tenant_id, inquiry)
    from app.models import Booking
    booking = (db.query(Booking).filter_by(tenant_id=tenant_id, inquiry_id=inquiry_id)
               .order_by(Booking.created_at.desc()).first())
    return {
        'inquiry_id': inquiry_id,
        'status': inquiry.status,
        'missing_fields': inquiry.missing_fields or [],
        'rfq_id': rfq.id if rfq else None,
        'quote_id': cq.id if cq else None,
        'quote_status': cq.status if cq else None,
        'sell_amount': cq.sell_amount if cq else None,
        'booking_id': booking.id if booking else None,
        'waiting_on': _waiting_on(inquiry, cq, booking),
        'checked_at': datetime.now(timezone.utc).isoformat(),
    }


def _waiting_on(inquiry, cq, booking) -> str | None:
    """Explain, in one word, what the pipeline is blocked on. Drives the UI badge."""
    if inquiry.missing_fields:
        return 'customer_clarification'
    if not cq:
        return 'supplier_quotes'
    if cq.status in {'waiting_approval', 'draft'}:
        return 'human_approval'
    if cq.status == 'approved':
        return 'quote_send'
    if cq.status == 'sent' and not booking:
        return 'booking'
    if booking:
        return None
    return None


__all__ = [
    'STAGES', 'GATED_STAGES', 'AUTO_STAGES', 'StageResult', 'PipelineRun',
    'run_pipeline', 'resume_after_approval', 'pipeline_status',
]
