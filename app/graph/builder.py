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
never becomes a second source of truth. There is no *in-memory* checkpointer -- one cannot
survive the deploy that happens while a quote waits for approval. `PipelineGraphRun` persists
the cursor, and `app/graph/checkpoint.py` persists real LangGraph checkpoints to SQL for
replay. Neither replaces the domain tables.

Who does what
-------------
Every step records the agent that owns the node (`app/agents/registry.py`), so a trace answers
"who acted" and not only "which function ran". Every side effect goes through
`app/harness/guard.py`, which refuses before the service is reached. The graph may be wrong
about sequencing and still cannot send an unapproved quote, because the refusal does not live
in the graph.
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

import app.harness as harness
from app.agents.registry import agent_for_node
from app.graph.nodes import guarded as _guarded
from app.graph.nodes import timed as _timed
from app.graph.nodes import trace_step as _step
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


def _halted(state: PipelineState) -> bool:
    return state.get('status') in HALT_STATUSES


def build_pipeline_graph(*, db, tenant_id: str, actor: str, trace_id: str,
                         roles: tuple[str, ...] = (), checkpointer=None):
    """Build a graph bound to one request's session.

    The session is captured in the closures rather than put in the state: graph state is
    serialised and logged, and a SQLAlchemy Session is neither serialisable nor safe to
    share that way.

    `roles` is the caller's role set, passed to the harness. It is empty by default so that
    an internal caller (a test, a script) is not silently granted a reviewer role; the HTTP
    layer always passes the principal's real roles.
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

    def _quote(state: PipelineState) -> CustomerQuote | None:
        """The quote row, not just its id.

        The harness needs the *status* to decide whether a send is allowed, and reading it
        from the row the service will act on is what keeps the decision and the action on the
        same object. Passing a status through graph state would let a stale value outlive the
        approval it described.
        """
        quote_id = _quote_id(state)
        if not quote_id:
            return None
        return db.query(CustomerQuote).filter_by(id=quote_id, tenant_id=tenant_id).first()

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
        row, results = await harness.run(
            'supplier.rfq.send', agent=agent_for_node('rfq'), db=db, tenant_id=tenant_id,
            actor=actor, roles=roles, detail={'inquiry_id': inquiry.id},
            call=lambda: rfq_svc.create_rfq(db, tenant_id, actor, trace_id, inquiry.id))
        return {'rfq_id': row.id,
                'steps': [_step('rfq', detail=f'sent to {len(results)} suppliers',
                                action='supplier.rfq.send', rfq_id=row.id)]}

    def supplier_quotes(state: PipelineState) -> dict:
        rfq_id = _rfq_id(state)
        if not rfq_id:
            raise ValueError('rfq_missing')
        have = db.query(SupplierQuote).filter_by(tenant_id=tenant_id, rfq_id=rfq_id).count()
        seeded = 0
        if state.get('allow_synthetic_quotes') and have < state.get('min_supplier_quotes', 2):
            # Recording a supplier quote is a DRAFT write, so the harness allows it -- but it
            # is still declared, still audited, and still owned by the sourcing agent. The
            # point of routing a harmless write through the gate is that the policy table
            # stays the complete surface; an exception here would be the crack a later
            # exception widens.
            harness.check('supplier.quote.record', agent=agent_for_node('supplier_quotes'),
                          db=db, tenant_id=tenant_id, actor=actor, roles=roles,
                          detail={'rfq_id': rfq_id, 'synthetic': True})
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
        # `quote.price` is DRAFT: the number is computed by the deterministic Pricing Engine.
        # The action is declared anyway, because "the agent may not alter the price" is
        # enforced by the engine, while "the agent may not price without being the quoting
        # agent" is enforced here.
        quote = await harness.run(
            'quote.price', agent=agent_for_node('optimize'), db=db, tenant_id=tenant_id,
            actor=actor, roles=roles, detail={'rfq_id': rfq_id},
            call=lambda: pricing_svc.optimize_quote(db, tenant_id, actor, trace_id, rfq_id))
        return {'rfq_id': rfq_id, 'quote_id': quote.id, 'sell_amount': quote.sell_amount,
                'steps': [_step('optimize', detail=f'sell {quote.sell_amount}',
                                action='quote.price',
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
        quote = _quote(state)
        if not quote:
            raise ValueError('quote_missing')
        # Two gates, deliberately not one. The harness refuses here, before the service is
        # called; `approval.send_quote` refuses again inside the service. Removing either
        # leaves the other working, which is why neither is "the" gate.
        await harness.run(
            'quote.send', agent=agent_for_node('quote_send'), db=db, tenant_id=tenant_id,
            actor=actor, roles=roles, approved=(quote.status == 'approved'),
            detail={'quote_id': quote.id},
            call=lambda: approval_svc.send_quote(db, tenant_id, actor, quote.id))
        return {'quote_id': quote.id,
                'steps': [_step('quote_send', detail='quote sent', action='quote.send',
                                quote_id=quote.id)]}

    async def booking(state: PipelineState) -> dict:
        quote = _quote(state)
        if not quote:
            raise ValueError('quote_missing')
        inquiry = _load(state)
        cargo = {'equipment': inquiry.equipment, 'quantity': inquiry.quantity,
                 'commodity': inquiry.commodity, 'weight_kg': inquiry.weight_kg}
        # `booking.create` is COMMITMENT, so approval is not optional: a booking is the one
        # action here that cannot be undone by editing a row. "Approved" means the quote was
        # actually sent -- a booking against an unsent quote is a commitment nobody promised.
        row, reused = await harness.run(
            'booking.create', agent=agent_for_node('booking'), db=db, tenant_id=tenant_id,
            actor=actor, roles=roles, approved=bool(quote.sent_at),
            detail={'quote_id': quote.id},
            call=lambda: booking_svc.create_booking(db, tenant_id, actor, inquiry.id,
                                                    quote.id, cargo))
        return {'steps': [_step('booking', detail=f'booking {row.status}'
                                + (' (existing)' if reused else ''),
                                action='booking.create',
                                booking_id=row.id, provider_ref=row.provider_ref,
                                reused=reused)]}

    async def vessel_tracking(state: PipelineState) -> dict:
        vessel = state.get('vessel_name')
        if not vessel:
            return {'steps': [_step('vessel_tracking', 'skipped', 'no vessel supplied')]}
        row = await harness.run(
            'vessel.position.refresh', agent=agent_for_node('vessel_tracking'), db=db,
            tenant_id=tenant_id, actor=actor, roles=roles, detail={'vessel': vessel},
            call=lambda: vessel_svc.refresh_position(db, tenant_id, actor, vessel))
        return {'steps': [_step('vessel_tracking', detail='position refreshed',
                                action='vessel.position.refresh',
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

    def _after_understand(state: PipelineState) -> str:
        # Order matters: `waiting_clarification` is itself a halt status, and `clarify` is
        # the node that emits the step explaining what is missing. Checking the halt first
        # would end the run without ever saying why.
        if state.get('status') == 'waiting_clarification':
            return 'clarify'
        return END if _halted(state) else 'schedules'

    graph.add_conditional_edges(
        'understand',
        _after_understand,
        {'clarify': 'clarify', 'schedules': 'schedules', END: END},
    )
    graph.add_edge('clarify', END)
    # Every remaining hop is conditional on "not halted", not a plain edge. A plain edge
    # here is how a failure at `understand` used to keep running schedules, rates, rfq and
    # supplier_quotes, each failing in turn, and report the *last* failure as the cause --
    # so a missing inquiry was diagnosed as a missing RFQ.
    for source, target in (('schedules', 'rates'), ('rates', 'rfq'), ('rfq', 'supplier_quotes'),
                           ('optimize', 'hitl_review')):
        graph.add_conditional_edges(source, _next(target), {target: target, END: END})
    graph.add_conditional_edges('supplier_quotes', _next('optimize'),
                                {'optimize': 'optimize', END: END})
    graph.add_conditional_edges('hitl_review', _next('quote_send'),
                                {'quote_send': 'quote_send', END: END})
    graph.add_conditional_edges('quote_send', _next('booking'),
                                {'booking': 'booking', END: END})
    graph.add_conditional_edges('booking', _next('vessel_tracking'),
                                {'vessel_tracking': 'vessel_tracking', END: END})
    graph.add_edge('vessel_tracking', END)
    return graph.compile(checkpointer=checkpointer)


# A node with no owning agent would produce a trace step nobody is accountable for, and a
# harness call with no identity to check against. Checked at import so a forgotten
# registration is a collection error in the suite, not an `agent: unknown` in production logs.
for _node in NODES:
    agent_for_node(_node)

