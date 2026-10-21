"""Latency and token-cost accounting over the runs the system already recorded.

Why this module exists
----------------------
`agent_runs` has carried `latency_ms`, `prompt_tokens`, `completion_tokens` and `cost_usd`
since the resilience work, and `pipeline_graph_runs.steps` carries per-node timings. Until
now nothing read them back. A recorded-but-unread field is the same failure as a test that
cannot fail: it looks like observability while answering no question. This module is the
reader, and `GET /api/v1/metrics/agent` is the surface.

Three rules the numbers follow
------------------------------
1. **Unreported is not zero.** The mock runtime consumes no tokens and the provider may not
   report usage at all. Those runs are counted in `unreported_runs` and excluded from the
   averages, because folding them in as 0 makes cost-per-run look excellent for the wrong
   reason -- the exact trap the nullable columns were added to avoid.
2. **Nothing is estimated.** No extrapolation, no "typical cost", no assumed token price.
   If the provider did not report it, the answer is `null`.
3. **Percentiles are nearest-rank over the sample, not interpolated.** With a handful of
   runs an interpolated p95 invents a value between two real observations, which is a
   number nobody measured.

Everything here is arithmetic over rows. No model is involved, and none should be: a
metric that an LLM produced cannot be used to argue about cost.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import AgentRun, PipelineGraphRun

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 90


def percentile(values: Iterable[float | int], q: float) -> float | None:
    """Nearest-rank percentile. `q` in (0, 100]. Returns None for an empty sample.

    Nearest-rank rather than linear interpolation: it always returns a value that actually
    occurred, so p95 of five runs is the slowest of those five, not a weighted invention
    between the fourth and fifth.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 < q <= 100:
        raise ValueError('percentile_quantile_out_of_range')
    rank = max(1, -(-len(ordered) * int(q * 100) // 10000))  # ceil(n * q / 100)
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True)
class Window:
    days: int
    since: datetime

    @classmethod
    def of(cls, days: int | None) -> 'Window':
        """Clamp the requested window instead of trusting it.

        An unbounded window turns a metrics endpoint into a full table scan of every run
        the tenant ever made -- a cheap denial of service on a dashboard left open.
        """
        requested = DEFAULT_WINDOW_DAYS if days is None else days
        if requested < 1:
            raise ValueError('metrics_window_out_of_range')
        days_ = min(requested, MAX_WINDOW_DAYS)
        return cls(days=days_, since=datetime.now(timezone.utc) - timedelta(days=days_))


def _latency_block(values: list[int]) -> dict[str, Any]:
    return {
        'samples': len(values),
        'p50_ms': percentile(values, 50),
        'p95_ms': percentile(values, 95),
        'max_ms': max(values) if values else None,
    }


def agent_metrics(db: Session, tenant_id: str, *, days: int | None = None) -> dict[str, Any]:
    """Token, cost and latency totals per agent, tenant-scoped.

    `cost_per_reported_run` divides by the runs that actually reported usage, never by all
    runs. The two denominators differ by design and the response names which one it used.
    """
    window = Window.of(days)
    rows = (db.query(AgentRun)
            .filter(AgentRun.tenant_id == tenant_id, AgentRun.created_at >= window.since)
            .all())

    reported = [r for r in rows if r.prompt_tokens is not None or r.completion_tokens is not None]
    costed = [r for r in reported if r.cost_usd is not None]

    by_agent: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = by_agent.setdefault(row.agent_name, {
            'agent_name': row.agent_name, 'runs': 0, 'failures': 0,
            'latencies': [], 'prompt_tokens': 0, 'completion_tokens': 0,
            'cost_usd': 0.0, 'unreported_runs': 0,
        })
        bucket['runs'] += 1
        bucket['failures'] += int(row.status != 'ok')
        bucket['latencies'].append(row.latency_ms or 0)
        if row.prompt_tokens is None and row.completion_tokens is None:
            bucket['unreported_runs'] += 1
        else:
            bucket['prompt_tokens'] += row.prompt_tokens or 0
            bucket['completion_tokens'] += row.completion_tokens or 0
            bucket['cost_usd'] += row.cost_usd or 0.0

    agents = []
    for bucket in sorted(by_agent.values(), key=lambda b: (-b['runs'], b['agent_name'])):
        latencies = bucket.pop('latencies')
        agents.append({**bucket, **_latency_block(latencies),
                       'cost_usd': round(bucket['cost_usd'], 6)})

    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1

    total_cost = round(sum(r.cost_usd for r in costed), 6)
    return {
        'window_days': window.days,
        'since': window.since.isoformat(),
        'runs': len(rows),
        'latency': _latency_block([r.latency_ms or 0 for r in rows]),
        'tokens': {
            'prompt': sum(r.prompt_tokens or 0 for r in reported),
            'completion': sum(r.completion_tokens or 0 for r in reported),
            'reported_runs': len(reported),
            'unreported_runs': len(rows) - len(reported),
        },
        'cost_usd': {
            'total': total_cost,
            # Null, not 0.0, when nothing reported a cost: "we do not know" and "it was
            # free" are different answers and a dashboard must not conflate them.
            'per_reported_run': round(total_cost / len(costed), 6) if costed else None,
            'reported_runs': len(costed),
        },
        'by_status': by_status,
        'by_agent': agents,
    }


def graph_metrics(db: Session, tenant_id: str, *, days: int | None = None) -> dict[str, Any]:
    """Per-node latency and halt distribution over persisted graph runs.

    Node timings are read out of the `steps` JSON rather than a separate table: the steps
    are the trace that explains a run, and duplicating them into a metrics table would
    create a second source of truth that drifts the moment a write path is missed.
    """
    window = Window.of(days)
    runs = (db.query(PipelineGraphRun)
            .filter(PipelineGraphRun.tenant_id == tenant_id,
                    PipelineGraphRun.created_at >= window.since)
            .all())

    per_node: dict[str, list[int]] = {}
    step_status: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    for run in runs:
        for step in run.steps or []:
            node = step.get('node')
            if not node:
                continue
            elapsed = step.get('elapsed_ms')
            if isinstance(elapsed, int):
                per_node.setdefault(node, []).append(elapsed)
            key = f"{node}:{step.get('status', 'ok')}"
            step_status[key] = step_status.get(key, 0) + 1
        # `stop_reason` has no column of its own: the cursor row stores the graph state, and
        # the reason is part of that state. Reading it from there keeps one source of truth
        # rather than adding a column that a write path could forget to fill.
        reason = (run.state or {}).get('stop_reason')
        if reason:
            stop_reasons[reason] = stop_reasons.get(reason, 0) + 1

    nodes = [{'node': node, **_latency_block(values)}
             for node, values in sorted(per_node.items())]

    by_status: dict[str, int] = {}
    for run in runs:
        by_status[run.status] = by_status.get(run.status, 0) + 1

    # A run that stopped at a gate is the normal case, not an error: the counts are kept
    # apart from `failed` on purpose so that "60% of runs are waiting for a human" reads as
    # a workflow fact rather than an incident.
    return {
        'window_days': window.days,
        'since': window.since.isoformat(),
        'runs': len(runs),
        'by_status': by_status,
        'stop_reasons': dict(sorted(stop_reasons.items(), key=lambda kv: -kv[1])),
        'step_status': dict(sorted(step_status.items(), key=lambda kv: -kv[1])),
        'nodes': nodes,
        'timed_runs': sum(1 for run in runs if any('elapsed_ms' in s for s in run.steps or [])),
    }
