"""LangGraph orchestration of the quote-to-booking workflow.

What the graph adds over the linear stage loop
---------------------------------------------
The existing `services/orchestrator.py` walks a fixed list. That is adequate for a demo and
insufficient for the real flow, which branches:

* an incomplete inquiry must stop and ask, not continue and fail later;
* a supplier round that has not produced enough quotes must wait, not price on one quote;
* the human gate must be a node with two exits, not an exception;
* a resumed run must re-enter at the node it stopped at.

Those are conditional edges and a re-enterable entry point, which is exactly what a state
graph gives you and a `for` loop does not.

What the graph deliberately does NOT do
--------------------------------------
It does not authorize anything. `quote_send` calls `approval.send_quote`, which refuses
unless the quote is approved; `booking` calls the booking service, which requires a sent
quote. The graph can be wrong about sequencing and still cannot send an unapproved quote.
The graph also never auto-approves: there is no `auto_approve` path here, unlike the legacy
orchestrator's demo flag.

State ownership
---------------
Business state stays in the domain tables (inquiry status, quote status, booking). The graph
state is a *cursor plus a trace*: it is recomputed from the database on every invocation and
never becomes a second source of truth. That is why there is no in-memory checkpointer --
an in-memory checkpoint cannot survive the deploy that happens while a quote waits for
approval. `PipelineGraphRun` persists the cursor instead.
"""
from __future__ import annotations

import operator
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from app.models import CustomerQuote, Inquiry, RFQ, SupplierQuote
from app.services import approval as approval_svc
from app.services import booking as booking_svc
from app.services import pricing as pricing_svc
from app.services import rates as rates_svc
from app.services import rfq as rfq_svc
from app.services import schedule as schedule_svc
from app.services import vessel as vessel_svc

NODES: tuple[str, ...] = (
    'understand', 'clarify', 'schedules', 'rates', 'rfq', 'supplier_quotes',
    'optimize', 'hitl_review', 'quote_send', 'booking', 'vessel_tracking',
)

# Where a run may (re-)enter. Resuming at the node it stopped at is the whole point.
ENTRY_NODES: frozenset[str] = frozenset(NODES) - {'clarify'}

HALT_STATUSES: frozenset[str] = frozenset({
    'waiting_clarification', 'waiting_supplier_quotes', 'waiting_approval', 'failed',
})

# Synthetic supplier quotes, used only when demo shortcuts are explicitly allowed.
_DEMO_QUOTES = (
    ('Ocean Partner A', 'a@carrier.demo', 2150.0, 140.0, 14, 7, 0.86),
    ('Ocean Partner B', 'b@carrier.demo', 1980.0, 165.0, 18, 4, 0.91),
)


class PipelineState(TypedDict, total=False):
    inquiry_id: str
    tenant_id: str
    actor: str
    trace_id: str
    start_node: str
    vessel_name: str | None
    allow_synthetic_quotes: bool
    min_supplier_quotes: int
    status: str
    stop_reason: str | None
    missing_fields: list[str]
    rate_count: int
    rfq_id: str | None
    supplier_quote_count: int
    quote_id: str | None
    sell_amount: float | None
    steps: Annotated[list[dict], operator.add]


def _step(node: str, status: str = 'ok', detail: str = '', elapsed_ms: int | None = None,
          **outputs) -> dict:
    step = {'node': node, 'status': status, 'detail': detail, 'outputs': outputs}
    if elapsed_ms is not None:
        step['elapsed_ms'] = elapsed_ms
    return step


def _ms(started: float) -> int:
    """Whole milliseconds since `started`, floored at 0.

    Rounded rather than truncated so a fast node does not read as 0 ms purely from
    float noise; the point of the field is to be honest about a slow node, not to
    flatter a fast one.
    """
    return max(0, round((time.perf_counter() - started) * 1000))


def _halted(state: PipelineState) -> bool:
    return state.get('status') in HALT_STATUSES


def _guarded(node: str, handler):
    """Run a node, converting a business refusal into a terminal state.

    A `ValueError` from a service is a *result* ("insufficient_supplier_quotes"), not a
    crash. Letting it propagate would abort the graph with a stack trace and lose the trace
    that explains which node refused.

    Every step produced here carries `elapsed_ms`. Timing lives in the wrapper rather than
    in each node so that adding a node cannot forget it, and so the failure paths are
    measured too -- a node that fails after 30 seconds is the one worth seeing.
    """
    import inspect

    async def wrapped(state: PipelineState) -> dict:
        started = time.perf_counter()
        try:
            result = handler(state)
            if inspect.isawaitable(result):
                result = await result
        except KeyError as exc:
            return {'status': 'failed', 'stop_reason': f'{node}:not_found:{exc}',
                    'steps': [_step(node, 'failed', f'not_found: {exc}',
                                    elapsed_ms=_ms(started))]}
        except ValueError as exc:
            return {'status': 'failed', 'stop_reason': f'{node}:{exc}',
                    'steps': [_step(node, 'failed', str(exc), elapsed_ms=_ms(started))]}

        elapsed = _ms(started)
        for step in result.get('steps') or []:
            step.setdefault('elapsed_ms', elapsed)
        return result

    return wrapped


def _timed(node: str, handler):
    """Measure a node that has no business refusal to catch.

    `clarify` only reshapes state, so it has nothing to guard -- but leaving it unmeasured
    would make it the one node that runs without a timing sample, and a metrics table that
    silently omits a node is worse than one that reports 0 ms for it.
    """

    async def wrapped(state: PipelineState) -> dict:
        started = time.perf_counter()
        result = handler(state)
        elapsed = _ms(started)
        for step in result.get('steps') or []:
            step.setdefault('elapsed_ms', elapsed)
        return result

    return wrapped


def build_pipeline_graph(*, db, tenant_id: str, actor: str, trace_id: str):
    """Build a graph bound to one request's session.

    The session is captured in the closures rather than put in the state: graph state is
    serialised and logged, and a SQLAlchemy Session is neither serialisable nor safe to
    share that way.
    """

    def _load(state: PipelineState) -> Inquiry:
        inquiry = (db.query(Inquiry)
                   .filter_by(id=state['inquiry_id'], tenant_id=tenant_id).first())
        if not inquiry:
            raise KeyError('inquiry_not_found')
        return inquiry

    def _rfq_id(state: PipelineState) -> str | None:
        """State is a cursor, the database is the truth.

        On a resumed run the graph is invoked fresh, so `rfq_id` is not in the incoming
        state. Reading it back from the database is what makes "resume at this node" work
        without a checkpointer replaying the earlier steps.
        """
        if state.get('rfq_id'):
            return state['rfq_id']
        row = (db.query(RFQ).filter_by(tenant_id=tenant_id, inquiry_id=state['inquiry_id'])
               .order_by(RFQ.created_at.desc()).first())
        return row.id if row else None

    def _quote_id(state: PipelineState) -> str | None:
        if state.get('quote_id'):
            return state['quote_id']
        row = (db.query(CustomerQuote)
               .filter_by(tenant_id=tenant_id, inquiry_id=state['inquiry_id'])
               .order_by(CustomerQuote.created_at.desc()).first())
        return row.id if row else None

    def understand(state: PipelineState) -> dict:
        inquiry = _load(state)
        if inquiry.missing_fields:
            return {
                'status': 'waiting_clarification',
                'stop_reason': 'inquiry_incomplete',
                'missing_fields': list(inquiry.missing_fields),
                'steps': [_step('understand', 'waiting_clarification', 'inquiry_incomplete',
                                missing_fields=list(inquiry.missing_fields))],
            }
        return {
            'missing_fields': [],
            'steps': [_step('understand', detail='inquiry complete',
                            origin=inquiry.origin, destination=inquiry.destination,
                            equipment=inquiry.equipment, etd=inquiry.etd)],
        }

    def clarify(state: PipelineState) -> dict:
        missing = state.get('missing_fields') or []
        return {'status': 'waiting_clarification', 'stop_reason': 'inquiry_incomplete',
                'steps': [_step('clarify', 'waiting_clarification',
                                f'need: {", ".join(missing) or "unknown"}',
                                missing_fields=missing)]}

    async def schedules(state: PipelineState) -> dict:
        inquiry = _load(state)
        rows = await schedule_svc.search_schedules(db, tenant_id, actor, inquiry.id)
        first = rows[0] if rows else None
        return {'steps': [_step('schedules', detail=f'{len(rows)} sailings',
                                schedule_count=len(rows),
                                vessel=first.vessel if first else None,
                                etd=first.etd if first else None)]}

    async def rates(state: PipelineState) -> dict:
        inquiry = _load(state)
        rows = await rates_svc.search_rates(db, tenant_id, actor, inquiry.id)
        cheapest = min(rows, key=lambda r: r.total_cost) if rows else None
        return {'rate_count': len(rows),
                'steps': [_step('rates', detail=f'{len(rows)} rates',
                                rate_count=len(rows),
                                cheapest_carrier=cheapest.carrier if cheapest else None,
                                cheapest_total=cheapest.total_cost if cheapest else None)]}

    async def rfq(state: PipelineState) -> dict:
        inquiry = _load(state)
        existing = (db.query(RFQ).filter_by(tenant_id=tenant_id, inquiry_id=inquiry.id)
                    .order_by(RFQ.created_at.desc()).first())
        if existing:
            return {'rfq_id': existing.id,
                    'steps': [_step('rfq', detail='reusing open rfq', rfq_id=existing.id)]}
        row, results = await rfq_svc.create_rfq(db, tenant_id, actor, trace_id, inquiry.id)
        return {'rfq_id': row.id,
                'steps': [_step('rfq', detail=f'sent to {len(results)} suppliers',
                                rfq_id=row.id)]}

    def supplier_quotes(state: PipelineState) -> dict:
        rfq_id = _rfq_id(state)
        if not rfq_id:
            raise ValueError('rfq_missing')
        have = db.query(SupplierQuote).filter_by(tenant_id=tenant_id, rfq_id=rfq_id).count()
        seeded = 0
        if state.get('allow_synthetic_quotes') and have < state.get('min_supplier_quotes', 2):
            from app.schemas import SupplierQuoteIn

            for name, addr, ocean, sur, transit, free, rel in _DEMO_QUOTES[have:]:
                rfq_svc.add_supplier_quote(db, tenant_id, actor, rfq_id, SupplierQuoteIn(
                    supplier_name=name, supplier_email=addr, ocean_freight=ocean,
                    surcharges=sur, transit_days=transit, free_time_days=free,
                    reliability_score=rel))
                seeded += 1

        total = db.query(SupplierQuote).filter_by(tenant_id=tenant_id, rfq_id=rfq_id).count()
        minimum = state.get('min_supplier_quotes', 2)
        if total < minimum:
            return {'supplier_quote_count': total, 'status': 'waiting_supplier_quotes',
                    'stop_reason': 'awaiting_supplier_quotes',
                    'steps': [_step('supplier_quotes', 'waiting_supplier_quotes',
                                    f'{total}/{minimum} quotes received', seeded=seeded)]}
        return {'supplier_quote_count': total,
                'steps': [_step('supplier_quotes', detail=f'{total} quotes', seeded=seeded)]}

    async def optimize(state: PipelineState) -> dict:
        rfq_id = _rfq_id(state)
        if not rfq_id:
            raise ValueError('rfq_missing')
        quote = await pricing_svc.optimize_quote(db, tenant_id, actor, trace_id, rfq_id)
        return {'rfq_id': rfq_id, 'quote_id': quote.id, 'sell_amount': quote.sell_amount,
                'steps': [_step('optimize', detail=f'sell {quote.sell_amount}',
                                quote_id=quote.id, cost=quote.cost_amount,
                                margin=quote.margin_amount)]}

    def hitl_review(state: PipelineState) -> dict:
        quote = (db.query(CustomerQuote)
                 .filter_by(tenant_id=tenant_id, inquiry_id=state['inquiry_id'])
                 .order_by(CustomerQuote.created_at.desc()).first())
        if not quote:
            raise ValueError('quote_missing')
        if quote.status == 'approved':
            return {'quote_id': quote.id,
                    'steps': [_step('hitl_review', detail='already approved',
                                    approved_by=quote.approved_by)]}
        if quote.status == 'rejected':
            raise ValueError('quote_rejected')
        return {
            'status': 'waiting_approval',
            'stop_reason': 'human_review_required',
            'quote_id': quote.id,
            'steps': [_step('hitl_review', 'waiting_approval', 'human_review_required',
                            quote_id=quote.id, sell_amount=quote.sell_amount,
                            resume_node='quote_send')],
        }

    async def quote_send(state: PipelineState) -> dict:
        quote_id = _quote_id(state)
        if not quote_id:
            raise ValueError('quote_missing')
        # The deterministic gate. Raises unless the quote is approved.
        await approval_svc.send_quote(db, tenant_id, actor, quote_id)
        return {'quote_id': quote_id,
                'steps': [_step('quote_send', detail='quote sent', quote_id=quote_id)]}

    async def booking(state: PipelineState) -> dict:
        quote_id = _quote_id(state)
        if not quote_id:
            raise ValueError('quote_missing')
        inquiry = _load(state)
        cargo = {'equipment': inquiry.equipment, 'quantity': inquiry.quantity,
                 'commodity': inquiry.commodity, 'weight_kg': inquiry.weight_kg}
        row, reused = await booking_svc.create_booking(db, tenant_id, actor, inquiry.id,
                                                       quote_id, cargo)
        return {'steps': [_step('booking', detail=f'booking {row.status}'
                                + (' (existing)' if reused else ''),
                                booking_id=row.id, provider_ref=row.provider_ref,
                                reused=reused)]}

    async def vessel_tracking(state: PipelineState) -> dict:
        vessel = state.get('vessel_name')
        if not vessel:
            return {'steps': [_step('vessel_tracking', 'skipped', 'no vessel supplied')]}
        row = await vessel_svc.refresh_position(db, tenant_id, actor, vessel)
        return {'steps': [_step('vessel_tracking', detail='position refreshed',
                                vessel=vessel, delay_hours=row.delay_hours)]}

    graph = StateGraph(PipelineState)
    graph.add_node('understand', _guarded('understand', understand))
    graph.add_node('clarify', _timed('clarify', clarify))
    graph.add_node('schedules', _guarded('schedules', schedules))
    graph.add_node('rates', _guarded('rates', rates))
    graph.add_node('rfq', _guarded('rfq', rfq))
    graph.add_node('supplier_quotes', _guarded('supplier_quotes', supplier_quotes))
    graph.add_node('optimize', _guarded('optimize', optimize))
    graph.add_node('hitl_review', _guarded('hitl_review', hitl_review))
    graph.add_node('quote_send', _guarded('quote_send', quote_send))
    graph.add_node('booking', _guarded('booking', booking))
    graph.add_node('vessel_tracking', _guarded('vessel_tracking', vessel_tracking))

    def _entry(state: PipelineState) -> str:
        node = state.get('start_node') or 'understand'
        return node if node in ENTRY_NODES else 'understand'

    graph.set_conditional_entry_point(_entry, {n: n for n in ENTRY_NODES})

    def _next(node: str):
        def route(state: PipelineState) -> str:
            return END if _halted(state) else node

        return route

    graph.add_conditional_edges(
        'understand',
        lambda s: 'clarify' if s.get('status') == 'waiting_clarification' else 'schedules',
        {'clarify': 'clarify', 'schedules': 'schedules'},
    )
    graph.add_edge('clarify', END)
    graph.add_edge('schedules', 'rates')
    graph.add_edge('rates', 'rfq')
    graph.add_edge('rfq', 'supplier_quotes')
    graph.add_conditional_edges('supplier_quotes', _next('optimize'),
                                {'optimize': 'optimize', END: END})
    graph.add_edge('optimize', 'hitl_review')
    graph.add_conditional_edges('hitl_review', _next('quote_send'),
                                {'quote_send': 'quote_send', END: END})
    graph.add_conditional_edges('quote_send', _next('booking'),
                                {'booking': 'booking', END: END})
    graph.add_conditional_edges('booking', _next('vessel_tracking'),
                                {'vessel_tracking': 'vessel_tracking', END: END})
    graph.add_edge('vessel_tracking', END)
    return graph.compile()
