"""The four agents, as identities rather than as prose in a diagram.

Before this module, "agent" meant a prompt in `prompts.py` or a node in the graph, and the
two were not connected. A trace showed `rfq` ran; it did not show *who* ran it, and nothing
prevented the acquisition path from reaching a node that sends prices to customers.

Each agent now declares what it owns and how far its authority reaches. Two invariants are
checked by `tests/test_agent_registry.py` rather than by a comment:

* every graph node is owned by exactly one agent, so a trace can always name the actor;
* an agent's `max_risk` covers every action it owns, so "the tracking agent only reads" is a
  property of the code and not of the documentation.

The orchestrator is not a fifth business agent. It routes, and it is limited to `READ`: it
can ask an agent to look something up, never to commit. That is the difference between an
orchestrator and a superuser.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.harness.risk import Risk


@dataclass(frozen=True)
class AgentSpec:
    name: str
    display: str
    mission: str
    owns: tuple[str, ...]
    max_risk: Risk

    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'display': self.display,
            'mission': self.mission,
            'owns': list(self.owns),
            'max_risk': int(self.max_risk),
            'max_risk_label': self.max_risk.label,
        }


AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec(
            'orchestrator', '编排器',
            '意图识别与任务路由。不做业务判断，不持有承诺级权限。',
            ('understand', 'clarify'), Risk.READ,
        ),
        AgentSpec(
            'acquisition', '获客 Agent',
            '候选企业发现、打分与触达准备。可以起草，不可以自行发信。',
            ('collect', 'score', 'qualify'), Risk.EXTERNAL,
        ),
        AgentSpec(
            'sourcing', '询价 Agent',
            '船期与运价检索、供应商 RFQ、回盘归集。',
            ('schedules', 'rates', 'rfq', 'supplier_quotes'), Risk.EXTERNAL,
        ),
        AgentSpec(
            'quoting', '报价 Agent',
            '成本与售价计算（调用确定性引擎）、审批闸门、发价与订舱。',
            ('optimize', 'hitl_review', 'quote_send', 'booking'), Risk.COMMITMENT,
        ),
        AgentSpec(
            'tracking', '跟踪 Agent',
            '船期与在途位置追踪、异常识别。只读，不改动任何业务状态。',
            ('vessel_tracking',), Risk.READ,
        ),
    )
}


def get_agent(name: str) -> AgentSpec:
    spec = AGENTS.get(name)
    if spec is None:
        raise KeyError(f'unknown_agent: {name}')
    return spec


def agent_for_node(node: str) -> str:
    """Which agent owns this graph node. Raises rather than guessing.

    A node with no owner is an orchestration defect: the trace would show a step nobody is
    accountable for, and the harness would have no identity to check against. Failing here
    (and at import time in `app/graph/builder.py`) turns that into a test failure instead of
    a mysterious `agent: unknown` in production logs.
    """
    for spec in AGENTS.values():
        if node in spec.owns:
            return spec.name
    raise KeyError(f'node_not_owned: {node}')


def nodes_of(agent: str) -> tuple[str, ...]:
    return get_agent(agent).owns


def agent_table() -> list[dict]:
    return [spec.as_dict() for spec in AGENTS.values()]


__all__ = ['AGENTS', 'AgentSpec', 'agent_for_node', 'agent_table', 'get_agent', 'nodes_of']
