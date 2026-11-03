"""The acquisition graph: discover, score, and decide who is reachable -- then stop.

Why this is a second graph and not three more nodes in `builder.py`
-------------------------------------------------------------------
获客 happens *before* an inquiry exists. `PipelineGraphRun.inquiry_id` is a non-null foreign
key, so folding acquisition into the quote graph would have meant a run pointing at an
inquiry that does not exist yet, or a nullable column every existing query would have to
start handling. Two graphs, one harness, one policy table is the smaller lie.

What the graph refuses to do
----------------------------
It does not send. `qualify` produces a list of candidates and stops. Sending is
`prospect.outreach.send`, an `EXTERNAL` action with a required reviewer role and a recorded
approval -- reachable only through `services/prospecting.send_outreach`, which has its own
four gates. A graph that could both find a stranger and email them in one step is the
failure mode this whole layer exists to prevent.

When nobody is reachable, the run stops with `no_reachable_prospect` and a breakdown of
*why*. "Found 50 companies, contacted 0" is the outcome an operator must be able to explain;
a silent zero is indistinguishable from a broken source.
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

import app.harness as harness
from app.agents.registry import agent_for_node
from app.config import get_settings
from app.graph.nodes import guarded as _guarded
from app.graph.nodes import trace_step as _step
from app.models import AcquisitionRun, ProspectCandidate
from app.services import prospecting as prospecting_svc
from app.services import sourcing as sourcing_svc
from app.services.common import audit, uid

NODES: tuple[str, ...] = ('collect', 'score', 'qualify')
ENTRY_NODES: frozenset[str] = frozenset(NODES)
HALT_STATUSES: frozenset[str] = frozenset({'no_source_configured', 'no_reachable_prospect',
                                           'failed'})

# Consent states that forbid contact regardless of score. `opted_out` is written by
# `mark_opt_out`; the others are vendor-supplied suppression flags.
_SUPPRESSED = frozenset({'opted_out', 'do_not_contact', 'unsubscribed'})


class AcquisitionState(TypedDict, total=False):
    tenant_id: str
    actor: str
    trace_id: str
    sources: str
    lane: str | None
    status: str
    stop_reason: str | None
    fetched: int
    accepted: int
    scored: int
    reachable: int
    gaps: dict
    candidates: list[dict]
    steps: Annotated[list[dict], operator.add]


def _halted(state: AcquisitionState) -> bool:
    return state.get('status') in HALT_STATUSES


def build_acquisition_graph(*, db, tenant_id: str, actor: str, trace_id: str,
                            roles: tuple[str, ...] = (), checkpointer=None):
    async def collect(state: AcquisitionState) -> dict:
        raw = state.get('sources')
        if raw is None:
            raw = get_settings().prospecting_sources
        specs = sourcing_svc.parse_sources(raw or '')
        if not specs:
            # Fail closed and say why. An empty source list used to mean "nothing happened",
            # which reads identically to "everything was already collected".
            return {'status': 'no_source_configured', 'stop_reason': 'no_source_configured',
                    'steps': [_step('collect', 'no_source_configured',
                                    'no source configured; nothing to collect')]}

        fetched = accepted = 0
        skipped: list[str] = []
        for spec in specs:
            harness.check('prospect.collect', agent=agent_for_node('collect'), db=db,
                          tenant_id=tenant_id, actor=actor, roles=roles,
                          detail={'source': spec.name, 'kind': spec.kind})
            result = await sourcing_svc.run_source(db, tenant_id, actor, spec)
            if 'skipped' in result:
                skipped.append(spec.name)
                continue
            fetched += int(result.get('fetched') or 0)
            accepted += int(result.get('accepted') or 0)

        return {'fetched': fetched, 'accepted': accepted,
                'steps': [_step('collect', action='prospect.collect',
                                detail=f'{len(specs)} source(s), {accepted}/{fetched} new',
                                sources=[s.name for s in specs], skipped=skipped)]}

    async def score(state: AcquisitionState) -> dict:
        s = get_settings()
        rows = (db.query(ProspectCandidate)
                .filter_by(tenant_id=tenant_id)
                .filter(ProspectCandidate.score == 0)
                .order_by(ProspectCandidate.created_at.desc())
                .limit(s.prospect_default_limit).all())
        for row in rows:
            await prospecting_svc.score_prospect(db, tenant_id, actor, trace_id, row.id)
        return {'scored': len(rows),
                'steps': [_step('score', detail=f'{len(rows)} candidate(s) scored',
                                scored=len(rows))]}

    def qualify(state: AcquisitionState) -> dict:
        s = get_settings()
        rows = (db.query(ProspectCandidate)
                .filter_by(tenant_id=tenant_id)
                .order_by(ProspectCandidate.score.desc())
                .limit(max(s.prospect_default_limit * 5, 100)).all())

        gaps = {'no_email': 0, 'below_floor': 0, 'suppressed': 0}
        ready: list[dict] = []
        for row in rows:
            if (row.consent_status or 'unknown') in _SUPPRESSED:
                gaps['suppressed'] += 1
            elif not (row.email or '').strip():
                gaps['no_email'] += 1
            elif (row.score or 0) < s.outreach_min_score:
                gaps['below_floor'] += 1
            else:
                ready.append({'prospect_id': row.id, 'company': row.company,
                              'email': row.email, 'score': row.score,
                              'trade_lane': row.trade_lane})

        if not ready:
            return {'status': 'no_reachable_prospect', 'stop_reason': 'no_reachable_prospect',
                    'reachable': 0, 'gaps': gaps,
                    'steps': [_step('qualify', 'no_reachable_prospect',
                                    'nobody is both reachable and worth contacting',
                                    considered=len(rows), gaps=gaps)]}

        return {'status': 'outreach_ready', 'reachable': len(ready), 'gaps': gaps,
                'candidates': ready,
                'steps': [_step('qualify', detail=f'{len(ready)} candidate(s) reachable',
                                action='prospect.outreach.draft',
                                considered=len(rows), gaps=gaps)]}

    graph = StateGraph(AcquisitionState)
    graph.add_node('collect', _guarded('collect', collect))
    graph.add_node('score', _guarded('score', score))
    graph.add_node('qualify', _guarded('qualify', qualify))
    graph.set_conditional_entry_point(
        lambda state: state.get('start_node') if state.get('start_node') in ENTRY_NODES
        else 'collect',
        {n: n for n in ENTRY_NODES})

    def _next(node: str):
        def route(state: AcquisitionState) -> str:
            return END if _halted(state) else node

        return route

    graph.add_conditional_edges('collect', _next('score'), {'score': 'score', END: END})
    graph.add_conditional_edges('score', _next('qualify'), {'qualify': 'qualify', END: END})
    graph.add_edge('qualify', END)
    return graph.compile(checkpointer=checkpointer)


async def run_acquisition(db, tenant_id: str, actor: str, trace_id: str, *,
                          sources: str | None = None, lane: str | None = None,
                          start_node: str = 'collect', roles: tuple[str, ...] = ()) -> dict:
    """Run the acquisition graph and persist a run row plus checkpoints."""
    if start_node not in NODES:
        raise ValueError(f'unknown_node: {start_node}')

    run_id = uid('arun_')
    thread_id = uid('athr_')
    from app.graph.checkpoint import SqlCheckpointSaver

    saver = SqlCheckpointSaver(db, tenant_id=tenant_id, run_id=run_id)
    graph = build_acquisition_graph(db=db, tenant_id=tenant_id, actor=actor,
                                    trace_id=trace_id, roles=tuple(roles),
                                    checkpointer=saver)
    initial: dict = {
        'tenant_id': tenant_id, 'actor': actor, 'trace_id': trace_id,
        'sources': sources, 'lane': lane, 'start_node': start_node,
        'status': 'running', 'steps': [],
    }
    final = await graph.ainvoke(initial, {'configurable': {'thread_id': thread_id}})
    steps = list(final.get('steps') or [])
    status = final.get('status') or 'completed'
    if status == 'running':
        status = 'completed'

    row = AcquisitionRun(
        id=run_id, tenant_id=tenant_id, thread_id=thread_id, status=status,
        stop_reason=final.get('stop_reason'),
        fetched=int(final.get('fetched') or 0), accepted=int(final.get('accepted') or 0),
        scored=int(final.get('scored') or 0), reachable=int(final.get('reachable') or 0),
        steps=steps, state={k: v for k, v in final.items() if k != 'steps'},
    )
    db.add(row)
    audit(db, tenant_id, actor, 'graph.acquisition', 'acquisition_run', run_id,
          {'status': status, 'stop_reason': final.get('stop_reason'),
           'reachable': row.reachable, 'gaps': final.get('gaps'),
           'nodes': [s['node'] for s in steps]})
    db.commit()

    return {'run_id': run_id, 'thread_id': thread_id, 'status': status,
            'stop_reason': final.get('stop_reason'), 'fetched': row.fetched,
            'accepted': row.accepted, 'scored': row.scored, 'reachable': row.reachable,
            'gaps': final.get('gaps') or {}, 'candidates': final.get('candidates') or [],
            'steps': steps}


__all__ = ['ENTRY_NODES', 'NODES', 'AcquisitionState', 'build_acquisition_graph',
           'run_acquisition']
