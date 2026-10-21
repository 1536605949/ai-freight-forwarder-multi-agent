"""Metrics: the reader for latency and token accounting that was previously write-only.

`agent_runs` has carried `latency_ms`, `prompt_tokens`, `completion_tokens` and `cost_usd`
since the resilience work, and `pipeline_graph_runs.steps` carries per-node timings. Nothing
read any of them back, so "we measure cost" was a claim about columns rather than a number
anyone could produce. These tests hold the reader to three properties that make the numbers
usable in an argument about production cost:

* a run whose provider reported nothing is **counted separately**, never averaged in as 0;
* a percentile is an observation, not an interpolation between two observations;
* one tenant's runs never appear in another tenant's totals.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AgentRun, Customer, Inquiry, PipelineGraphRun, Tenant
from app.services.metrics import (
    MAX_WINDOW_DAYS,
    Window,
    agent_metrics,
    graph_metrics,
    percentile,
)

TENANT = 'tenant-demo'
OTHER = 'tenant-other'


@pytest.fixture
def mdb():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([Tenant(id=TENANT, name='Demo'), Tenant(id=OTHER, name='Other')])
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _run(db, run_id: str, *, agent: str = 'extractor', latency_ms: int = 100,
         prompt: int | None = None, completion: int | None = None,
         cost: float | None = None, status: str = 'ok', tenant: str = TENANT) -> None:
    db.add(AgentRun(id=run_id, tenant_id=tenant, agent_name=agent, trace_id='t1',
                    input_summary='x', status=status, latency_ms=latency_ms,
                    prompt_tokens=prompt, completion_tokens=completion, cost_usd=cost,
                    model='mock' if prompt is not None else None))
    db.commit()


def _graph_run(db, run_id: str, steps: list[dict], *, status: str = 'running',
               stop_reason: str | None = None, tenant: str = TENANT) -> None:
    db.add(Customer(id=f'cus_{run_id}', tenant_id=tenant, company='Acme',
                    email=f'{run_id}@acme.test'))
    db.add(Inquiry(id=f'inq_{run_id}', tenant_id=tenant, customer_id=f'cus_{run_id}',
                   raw_message='x', origin='Shanghai', destination='Los Angeles',
                   equipment='40HQ', quantity=1, etd='2026-09-25', missing_fields=[],
                   status='ready', external_message_id=f'm_{run_id}'))
    db.add(PipelineGraphRun(id=run_id, tenant_id=tenant, inquiry_id=f'inq_{run_id}',
                            thread_id=run_id, status=status, steps=steps,
                            state={'stop_reason': stop_reason} if stop_reason else {}))
    db.commit()


# --------------------------------------------------------------------------- #
# Percentile: hand-computed expectations, not a comparison with itself
# --------------------------------------------------------------------------- #

def test_percentile_is_nearest_rank_not_interpolated():
    # n=4: ceil(0.95*4)=4 -> the 4th value. An interpolating p95 would land between
    # 300 and 400 and report something nobody observed.
    assert percentile([100, 200, 300, 400], 95) == 400
    # n=20: ceil(0.95*20)=19 -> the 19th value, not the 20th.
    assert percentile(list(range(1, 21)), 95) == 19
    assert percentile([5, 1, 3], 50) == 3
    assert percentile([7], 95) == 7


def test_percentile_of_an_empty_sample_is_none_not_zero():
    assert percentile([], 95) is None
    assert percentile([], 50) is None


def test_percentile_rejects_a_quantile_outside_the_range():
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 0)
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 101)


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #

def test_window_is_clamped_so_a_dashboard_cannot_scan_everything():
    assert Window.of(None).days == 7
    assert Window.of(10_000).days == MAX_WINDOW_DAYS
    with pytest.raises(ValueError):
        Window.of(0)


# --------------------------------------------------------------------------- #
# Agent metrics
# --------------------------------------------------------------------------- #

def test_unreported_usage_is_excluded_from_the_average_and_counted_separately(mdb):
    _run(mdb, 'r1', prompt=1000, completion=500, cost=0.02, latency_ms=100)
    _run(mdb, 'r2', latency_ms=300)  # mock runtime: no usage reported at all

    m = agent_metrics(mdb, TENANT)

    assert m['runs'] == 2
    assert m['tokens'] == {'prompt': 1000, 'completion': 500,
                           'reported_runs': 1, 'unreported_runs': 1}
    # The denominator is the one run that reported, so the average is 0.02 -- not 0.01,
    # which is what folding the silent run in as zero would have produced.
    assert m['cost_usd'] == {'total': 0.02, 'per_reported_run': 0.02, 'reported_runs': 1}
    assert m['latency'] == {'samples': 2, 'p50_ms': 100, 'p95_ms': 300, 'max_ms': 300}


def test_cost_per_run_is_null_when_nothing_reported_a_cost(mdb):
    _run(mdb, 'r1', latency_ms=100)

    m = agent_metrics(mdb, TENANT)

    # "unknown" and "free" are different answers. A 0.0 here would make the endpoint
    # claim the platform runs at zero cost.
    assert m['cost_usd']['per_reported_run'] is None
    assert m['cost_usd']['total'] == 0.0
    assert m['cost_usd']['reported_runs'] == 0


def test_agent_breakdown_is_per_agent_and_counts_failures(mdb):
    _run(mdb, 'r1', agent='extractor', latency_ms=100)
    _run(mdb, 'r2', agent='extractor', latency_ms=500)
    _run(mdb, 'r3', agent='responder', latency_ms=200, status='error')

    m = agent_metrics(mdb, TENANT)
    by_name = {a['agent_name']: a for a in m['by_agent']}

    assert by_name['extractor']['runs'] == 2
    assert by_name['extractor']['p95_ms'] == 500
    assert by_name['responder']['failures'] == 1
    assert m['by_status'] == {'ok': 2, 'error': 1}
    # Sorted by run count, so the busiest agent is the first row a dashboard shows.
    assert m['by_agent'][0]['agent_name'] == 'extractor'


def test_one_tenant_never_sees_another_tenants_runs(mdb):
    _run(mdb, 'r1', latency_ms=100, prompt=10, completion=10, cost=1.0)
    _run(mdb, 'r2', latency_ms=9000, prompt=999, completion=999, cost=99.0, tenant=OTHER)

    m = agent_metrics(mdb, TENANT)

    assert m['runs'] == 1
    assert m['latency']['max_ms'] == 100
    assert m['cost_usd']['total'] == 1.0


def test_empty_window_reports_zeros_and_nulls_rather_than_failing(mdb):
    m = agent_metrics(mdb, TENANT)

    assert m['runs'] == 0
    assert m['latency']['p95_ms'] is None
    assert m['by_agent'] == []
    assert m['cost_usd']['per_reported_run'] is None


# --------------------------------------------------------------------------- #
# Graph metrics
# --------------------------------------------------------------------------- #

def test_graph_metrics_percentiles_come_from_the_recorded_steps(mdb):
    _graph_run(mdb, 'g1', [
        {'node': 'understand', 'status': 'ok', 'elapsed_ms': 10},
        {'node': 'rates', 'status': 'ok', 'elapsed_ms': 100},
    ], status='waiting_approval', stop_reason='human_review_required')
    _graph_run(mdb, 'g2', [
        {'node': 'understand', 'status': 'ok', 'elapsed_ms': 30},
        {'node': 'rates', 'status': 'failed', 'elapsed_ms': 300},
    ], status='failed', stop_reason='rates:no_rates')

    m = graph_metrics(mdb, TENANT)
    nodes = {n['node']: n for n in m['nodes']}

    assert m['runs'] == 2
    assert nodes['understand']['samples'] == 2
    assert nodes['understand']['p50_ms'] == 10
    assert nodes['rates']['p95_ms'] == 300
    assert m['by_status'] == {'waiting_approval': 1, 'failed': 1}
    # A gate stop and a failure are counted apart: waiting for a human is a workflow
    # fact, not an incident.
    assert m['stop_reasons'] == {'human_review_required': 1, 'rates:no_rates': 1}
    assert m['step_status']['rates:failed'] == 1


def test_steps_without_a_timing_are_not_counted_as_zero_milliseconds(mdb):
    _graph_run(mdb, 'g1', [{'node': 'understand', 'status': 'ok'}])

    m = graph_metrics(mdb, TENANT)

    # The step is still visible in step_status -- it happened -- but it contributes no
    # latency sample, because "we did not time it" is not "it took 0 ms".
    assert m['nodes'] == []
    assert m['step_status'] == {'understand:ok': 1}
    assert m['timed_runs'] == 0


def test_graph_metrics_are_tenant_scoped(mdb):
    _graph_run(mdb, 'g1', [{'node': 'understand', 'status': 'ok', 'elapsed_ms': 10}])
    _graph_run(mdb, 'g2', [{'node': 'understand', 'status': 'ok', 'elapsed_ms': 9999}],
               tenant=OTHER)

    m = graph_metrics(mdb, TENANT)

    assert m['runs'] == 1
    assert m['nodes'] == [{'node': 'understand', 'samples': 1,
                           'p50_ms': 10, 'p95_ms': 10, 'max_ms': 10}]


# --------------------------------------------------------------------------- #
# The endpoint, and the proof that the graph actually fills these fields
# --------------------------------------------------------------------------- #

@pytest.fixture
def mclient(mdb):
    app.dependency_overrides[get_db] = lambda: mdb
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_metrics_endpoint_returns_both_blocks(mclient, mdb):
    _run(mdb, 'r1', latency_ms=120, prompt=100, completion=50, cost=0.01)
    _graph_run(mdb, 'g1', [{'node': 'understand', 'status': 'ok', 'elapsed_ms': 40}])

    resp = mclient.get('/api/v1/metrics/agent', headers={'X-Demo-User': 'u1',
                                                         'X-Demo-Tenant': TENANT})

    assert resp.status_code == 200
    body = resp.json()
    assert body['agents']['runs'] == 1
    assert body['graph']['nodes'][0]['node'] == 'understand'


def test_metrics_endpoint_rejects_an_impossible_window(mclient):
    resp = mclient.get('/api/v1/metrics/agent?days=0',
                       headers={'X-Demo-User': 'u1', 'X-Demo-Tenant': TENANT})

    assert resp.status_code == 422
    assert resp.json()['detail'] == 'metrics_window_out_of_range'


@pytest.mark.asyncio
async def test_a_real_graph_run_fills_the_timing_the_metrics_read(db):
    """The wiring test: not "the field exists" but "the graph writes it".

    Without this, `elapsed_ms` could be a field that only the unit tests above ever set,
    and the endpoint would be permanently empty in production while every test passed.
    """
    from app.graph import run_graph

    db.add(Customer(id='cus_m', tenant_id=TENANT, company='Acme', email='m@acme.test'))
    db.add(Inquiry(id='inq_metrics', tenant_id=TENANT, customer_id='cus_m',
                   raw_message='1x40HQ Shanghai to Los Angeles, ETD 2026-09-25',
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ',
                   quantity=1, etd='2026-09-25', missing_fields=[], status='ready',
                   external_message_id='m_metrics'))
    db.commit()

    run = await run_graph(db, TENANT, 'u1', 'trace-metrics', 'inq_metrics')

    assert run.steps, 'the graph produced no steps at all'
    assert all('elapsed_ms' in s for s in run.steps), \
        [s['node'] for s in run.steps if 'elapsed_ms' not in s]

    m = graph_metrics(db, TENANT)
    assert m['timed_runs'] == 1
    assert {n['node'] for n in m['nodes']} >= {'understand', 'schedules', 'rates'}
