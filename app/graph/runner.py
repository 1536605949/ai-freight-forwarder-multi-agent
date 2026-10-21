"""Execute and resume the pipeline graph, persisting the cursor.

Resume semantics
----------------
A run that stopped at a gate is resumed by invoking the graph again with `start_node` set to
the node after the gate. The conditional entry point makes that a first-class path rather
than a special case, so "resume" and "run" share one implementation.

The alternative -- keeping a live graph in memory and calling `update_state` -- was
rejected: a quote can wait hours for a reviewer, and an in-memory checkpoint does not
survive the deploy that happens in between. The database is the durable store; this module
writes the cursor to `pipeline_graph_runs` after every invocation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.graph.builder import NODES, build_pipeline_graph
from app.models import PipelineGraphRun
from app.services.common import audit, uid

# Which node a halted run should re-enter at, given why it stopped.
RESUME_POINTS: dict[str, str] = {
    'waiting_clarification': 'understand',
    'waiting_supplier_quotes': 'supplier_quotes',
    'waiting_approval': 'quote_send',
}

_TERMINAL_FAILURE_NODES = {'understand', 'schedules', 'rates', 'rfq', 'supplier_quotes',
                           'optimize', 'hitl_review', 'quote_send', 'booking',
                           'vessel_tracking'}


@dataclass
class GraphRun:
    run_id: str
    thread_id: str
    inquiry_id: str
    status: str
    stop_reason: str | None
    steps: list[dict] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    resume_node: str | None = None
    synthetic_quotes_allowed: bool = False

    def as_dict(self) -> dict:
        return {
            'run_id': self.run_id,
            'thread_id': self.thread_id,
            'inquiry_id': self.inquiry_id,
            'status': self.status,
            'stop_reason': self.stop_reason,
            'resume_node': self.resume_node,
            'synthetic_quotes_allowed': self.synthetic_quotes_allowed,
            'steps': self.steps,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _effective_demo_shortcuts(requested: bool | None) -> bool:
    """Production can never seed synthetic supplier quotes, whatever the caller asks for.

    The legacy orchestrator took a per-request `auto_supplier_quotes` flag defaulting to
    True, which means a production caller that simply omitted it got synthetic quotes in a
    real quote comparison. That is a pricing-integrity defect, not a demo convenience.
    """
    s = get_settings()
    if s.environment == 'production':
        return False
    if requested is None:
        return s.allow_demo_shortcuts
    return bool(requested)


def _resume_node(status: str, steps: list[dict]) -> str | None:
    if status in RESUME_POINTS:
        return RESUME_POINTS[status]
    if status == 'failed':
        failed = [s['node'] for s in steps if s.get('status') == 'failed']
        if failed and failed[-1] in _TERMINAL_FAILURE_NODES:
            return failed[-1]
    return None


async def run_graph(
    db: Session,
    tenant_id: str,
    actor: str,
    trace_id: str,
    inquiry_id: str,
    *,
    start_node: str = 'understand',
    vessel_name: str | None = None,
    allow_synthetic_quotes: bool | None = None,
) -> GraphRun:
    if start_node not in NODES:
        raise ValueError(f'unknown_node: {start_node}')
    if start_node == 'clarify':
        raise ValueError('clarify is a terminal node and cannot be a start node')

    s = get_settings()
    allow_synthetic = _effective_demo_shortcuts(allow_synthetic_quotes)

    graph = build_pipeline_graph(db=db, tenant_id=tenant_id, actor=actor, trace_id=trace_id)
    initial: dict[str, Any] = {
        'inquiry_id': inquiry_id,
        'tenant_id': tenant_id,
        'actor': actor,
        'trace_id': trace_id,
        'start_node': start_node,
        'vessel_name': vessel_name,
        'allow_synthetic_quotes': allow_synthetic,
        'min_supplier_quotes': s.min_supplier_quotes,
        'status': 'running',
        'steps': [],
    }

    final = await graph.ainvoke(initial)
    steps = list(final.get('steps') or [])
    status = final.get('status') or 'completed'
    if status == 'running':
        status = 'completed'

    run_id = uid('grun_')
    thread_id = uid('thr_')
    record = PipelineGraphRun(
        id=run_id, tenant_id=tenant_id, inquiry_id=inquiry_id, thread_id=thread_id,
        status=status, current_node=steps[-1]['node'] if steps else start_node,
        pending_gate=final.get('stop_reason') if status.startswith('waiting') else None,
        steps=steps,
        state={k: v for k, v in final.items() if k != 'steps'},
    )
    db.add(record)
    audit(db, tenant_id, actor, 'graph.run', 'inquiry', inquiry_id,
          {'run_id': run_id, 'status': status, 'start_node': start_node,
           'stop_reason': final.get('stop_reason'),
           'nodes': [st['node'] for st in steps],
           'synthetic_quotes_allowed': allow_synthetic})
    db.commit()

    return GraphRun(run_id=run_id, thread_id=thread_id, inquiry_id=inquiry_id,
                    status=status, stop_reason=final.get('stop_reason'), steps=steps,
                    state={k: v for k, v in final.items() if k != 'steps'},
                    resume_node=_resume_node(status, steps),
                    synthetic_quotes_allowed=allow_synthetic)


async def resume_graph(
    db: Session,
    tenant_id: str,
    actor: str,
    trace_id: str,
    inquiry_id: str,
    *,
    vessel_name: str | None = None,
) -> GraphRun:
    """Re-enter a halted run at the node it stopped at.

    The node is derived from *persisted state*, not from the caller: a client cannot ask to
    resume at `quote_send` when the run actually stopped waiting for clarification.
    """
    last = latest_run(db, tenant_id, inquiry_id)
    if last is None:
        raise KeyError('no_run_for_inquiry')
    node = _resume_node(last.status, last.steps)
    if node is None:
        raise ValueError(f'run_is_not_resumable: {last.status}')
    return await run_graph(db, tenant_id, actor, trace_id, inquiry_id,
                           start_node=node, vessel_name=vessel_name)


def latest_run(db: Session, tenant_id: str, inquiry_id: str) -> PipelineGraphRun | None:
    return (db.query(PipelineGraphRun)
            .filter_by(tenant_id=tenant_id, inquiry_id=inquiry_id)
            .order_by(PipelineGraphRun.created_at.desc(), PipelineGraphRun.id.desc())
            .first())
