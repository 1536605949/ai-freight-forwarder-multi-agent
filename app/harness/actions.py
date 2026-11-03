"""Every side effect the system performs, declared once with its owner and its risk.

This table is the answer to "what can each agent actually do?". It is deliberately a data
structure rather than a set of `if` statements spread through the graph: a reviewer can read
one file and see the complete surface, and the guard can refuse anything that is not in it.

Ownership is single. `quote.send` belongs to the quoting agent and to nobody else, so an
acquisition agent that tries to call it is refused for a reason that names the rule it broke
rather than a generic permission error. The orchestrator is the one exception and it may only
touch `READ` actions -- routing is not authority.

Note the split between `quote.price` and `quote.send`. Pricing is `DRAFT` because the number
is computed by a deterministic engine and stored; sending is `EXTERNAL` and requires a human
approval that was recorded *before* the call. Collapsing them into one action is how a model
ends up able to price and send in a single step.

The `roles` field is checked before approval on purpose: "you are not the right person" is
not something an approval can fix.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.harness.risk import Risk


@dataclass(frozen=True)
class ActionSpec:
    name: str
    agent: str
    risk: Risk
    description: str
    requires_approval: bool = False
    roles: tuple[str, ...] = ()

    @property
    def approval_required(self) -> bool:
        """`COMMITMENT` can never be configured to skip approval.

        Written as a property rather than a default so that a future action declared at
        `COMMITMENT` cannot accidentally opt out by leaving `requires_approval=False`. The
        worst level is not a per-action choice.
        """
        return self.requires_approval or self.risk >= Risk.COMMITMENT

    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'agent': self.agent,
            'risk': int(self.risk),
            'risk_label': self.risk.label,
            'requires_approval': self.approval_required,
            'roles': list(self.roles),
            'description': self.description,
        }


ACTIONS: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec('prospect.collect', 'acquisition', Risk.READ,
                   '从公开名录或订阅源增量采集候选企业'),
        ActionSpec('prospect.outreach.draft', 'acquisition', Risk.DRAFT,
                   '生成开发信草稿，不发送'),
        ActionSpec('prospect.outreach.send', 'acquisition', Risk.EXTERNAL,
                   '向潜客发送开发信（合规敏感，需复核人角色 + 审批）',
                   requires_approval=True, roles=('reviewer',)),
        ActionSpec('supplier.rfq.send', 'sourcing', Risk.EXTERNAL,
                   '向供应商发出 RFQ'),
        ActionSpec('supplier.quote.record', 'sourcing', Risk.DRAFT,
                   '记录供应商回盘'),
        ActionSpec('quote.price', 'quoting', Risk.DRAFT,
                   '由确定性 Pricing Engine 计算成本与售价'),
        ActionSpec('quote.send', 'quoting', Risk.EXTERNAL,
                   '向客户发送报价单', requires_approval=True),
        ActionSpec('booking.create', 'quoting', Risk.COMMITMENT,
                   '向船公司订舱（不可逆承诺）'),
        ActionSpec('vessel.position.refresh', 'tracking', Risk.READ,
                   '刷新船舶位置'),
    )
}


def get_action(name: str) -> ActionSpec | None:
    return ACTIONS.get(name)


def action_names() -> list[str]:
    return sorted(ACTIONS)


__all__ = ['ACTIONS', 'ActionSpec', 'action_names', 'get_action']
