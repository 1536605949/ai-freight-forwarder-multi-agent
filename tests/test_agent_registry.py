"""The four agents as identities: who owns which node, and how far each may reach.

The point of these tests is that the diagram is checkable. "获客 / 询价 / 报价 / 跟踪" are not
labels on a slide; each one owns a set of graph nodes, and each one's authority ceiling is
compared against the risk of the actions it is allowed to perform.
"""
from __future__ import annotations

import pytest

from app.agents.registry import AGENTS, agent_for_node, agent_table, get_agent, nodes_of
from app.graph import acquisition as acquisition_graph
from app.graph.builder import NODES as PIPELINE_NODES
from app.harness.actions import ACTIONS
from app.harness.risk import Risk


ALL_GRAPH_NODES = tuple(PIPELINE_NODES) + tuple(acquisition_graph.NODES)


def test_every_graph_node_has_exactly_one_owner():
    owners: dict[str, list[str]] = {}
    for agent, spec in AGENTS.items():
        for node in spec.owns:
            owners.setdefault(node, []).append(agent)

    shared = {node: agents for node, agents in owners.items() if len(agents) > 1}
    assert not shared, f'node(s) claimed by more than one agent: {shared}'

    unowned = [node for node in ALL_GRAPH_NODES if node not in owners]
    assert not unowned, (
        f'graph node(s) with no owning agent: {unowned}. A step nobody owns cannot be '
        f'audited, and the harness would have no identity to check.')


def test_an_unowned_node_raises_instead_of_guessing():
    with pytest.raises(KeyError, match='node_not_owned'):
        agent_for_node('send_everything')


def test_an_unknown_agent_raises():
    with pytest.raises(KeyError, match='unknown_agent'):
        get_agent('growth_hacker')


def test_each_agents_ceiling_covers_the_actions_it_owns():
    """`tracking` is declared read-only. If someone hands it a committing action, this fails.

    Without this assertion the `max_risk` field would be decoration -- and decoration that
    says "this agent cannot commit" is worse than no field at all.
    """
    problems = []
    for action in ACTIONS.values():
        agent = get_agent(action.agent)
        if agent.max_risk < action.risk:
            problems.append(
                f'{action.name} is {action.risk.label} but {agent.name} may only reach '
                f'{agent.max_risk.label}')
    assert not problems, '\n'.join(problems)


def test_the_orchestrator_holds_read_only_authority():
    assert AGENTS['orchestrator'].max_risk == Risk.READ
    assert AGENTS['orchestrator'].owns == ('understand', 'clarify')


def test_the_tracking_agent_owns_only_read_actions():
    owned = [a for a in ACTIONS.values() if a.agent == 'tracking']
    assert owned, 'the tracking agent must own at least one declared action'
    assert all(a.risk == Risk.READ for a in owned)


def test_the_four_business_agents_are_present():
    assert {'acquisition', 'sourcing', 'quoting', 'tracking'} <= set(AGENTS)
    assert nodes_of('sourcing') == ('schedules', 'rates', 'rfq', 'supplier_quotes')


def test_the_agent_table_is_serialisable_for_the_endpoint():
    table = agent_table()
    assert {row['name'] for row in table} == set(AGENTS)
    for row in table:
        assert isinstance(row['max_risk'], int)
        assert isinstance(row['owns'], list)


@pytest.mark.asyncio
async def test_every_step_of_a_real_run_names_its_agent(db):
    """The trace must answer "who acted", not only "which function ran"."""
    from app.models import Customer, Inquiry
    from app.graph import run_graph

    db.add(Customer(id='cus_agent', tenant_id='tenant-demo', company='Acme',
                    email='a@acme-import.com'))
    db.add(Inquiry(id='inq_agent', tenant_id='tenant-demo', customer_id='cus_agent',
                   raw_message='1x40HQ Shanghai to Los Angeles',
                   origin='Shanghai', destination='Los Angeles', equipment='40HQ',
                   quantity=1, etd='2026-09-25', missing_fields=[], status='ready',
                   external_message_id='m-agent'))
    db.commit()

    run = await run_graph(db, 'tenant-demo', 'u', 't', 'inq_agent',
                          allow_synthetic_quotes=True)

    assert run.steps, 'the run produced no steps at all'
    for step in run.steps:
        assert step['agent'] in AGENTS, f"step {step['node']} has agent {step['agent']!r}"
        assert step['agent'] == agent_for_node(step['node'])

    agents_seen = [s['agent'] for s in run.steps]
    assert agents_seen == ['orchestrator', 'sourcing', 'sourcing', 'sourcing', 'sourcing',
                           'quoting', 'quoting']
