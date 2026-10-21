"""Bounded tool-calling loop.

The loop is model-agnostic: it asks a *policy* what to call next, executes through the
registry, and stops on a fixed budget. Two policies ship with it:

* `DeterministicToolPolicy` -- maps an explicit question to at most two read-only tools.
  This is what runs in mock mode, so the whole path is testable and demoable with no key.
* `ScriptedToolPolicy` -- replays a fixed list of calls. Used by tests to prove the loop
  terminates, that a rejected call is surfaced rather than swallowed, and that the step
  budget is enforced.

An `OpenAIToolPolicy` would be a third implementation of the same two-method interface. It
is not shipped enabled because the point of this module is the *guardrails*, and those are
identical whichever model does the choosing.

Why a budget
------------
A model that keeps asking for another lookup is indistinguishable from a hung request. Four
steps is enough for "read the inquiry, then read the SOP" and short enough that a loop
cannot burn a token budget before anyone notices.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from app.agents.tools import ToolContext, ToolError, ToolRegistry, registry, summarize
from app.config import get_settings

_INQUIRY_ID = re.compile(r'\b(inq_[0-9a-f]{6,})\b')
_ID_ANYWHERE = re.compile(r'\b(?:inq|cq|bkg|rfq|run|grun)_[0-9a-f]{6,}\b')


def _clean_question(question: str) -> str:
    """Remove object ids before a retrieval query.

    An identifier is not a topic word. Passing "inq_abc123def456 这票货的订舱流程是什么" to
    the SOP lookup dragged the domain-coverage ratio below the gate and produced a refusal
    for a perfectly answerable question -- the id's 15 characters counted as unknown content.
    """
    return _ID_ANYWHERE.sub(' ', question).strip()

_SCHEDULE_HINTS = ('船期', '开船', '到港', '航次', '船名', 'sailing')
_RATE_HINTS = ('运价', '报价', '价格', '多少钱', '运费', 'rate', 'price')
_QUOTE_HINTS = ('报价单', '审批', '报价状态', 'quote')


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass
class ToolStep:
    name: str
    arguments: dict
    ok: bool
    result: dict | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {'tool': self.name, 'arguments': self.arguments, 'ok': self.ok,
                'result': self.result if self.ok else None,
                'error': self.error}


@dataclass
class ToolLoopResult:
    question: str
    answer: str
    steps: list[ToolStep] = field(default_factory=list)
    stopped_reason: str = 'policy_done'
    citations: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {'question': self.question, 'answer': self.answer,
                'stopped_reason': self.stopped_reason,
                'citations': self.citations,
                'steps': [s.as_dict() for s in self.steps]}


class ToolPolicy(Protocol):
    async def next_call(self, question: str, steps: list[ToolStep],
                        schema: list[dict]) -> ToolCall | None: ...


class DeterministicToolPolicy:
    """Offline policy: pick a read-only lookup from explicit cues in the question.

    It reads the inquiry id from the text rather than inventing one. When the question
    mentions a business object but supplies no id, it does not guess -- it falls back to the
    SOP lookup, which is the only tool that needs no identifier.
    """

    name = 'deterministic'

    async def next_call(self, question: str, steps: list[ToolStep],
                        schema: list[dict]) -> ToolCall | None:
        called = {s.name for s in steps}
        match = _INQUIRY_ID.search(question)
        inquiry_id = match.group(1) if match else None
        low = question.lower()

        if inquiry_id and 'get_inquiry' not in called:
            return ToolCall('get_inquiry', {'inquiry_id': inquiry_id})
        if inquiry_id and any(h in low for h in _QUOTE_HINTS) and 'get_quote_status' not in called:
            return ToolCall('get_quote_status', {'inquiry_id': inquiry_id})
        if inquiry_id and any(h in low for h in _RATE_HINTS) and 'list_rate_quotes' not in called:
            return ToolCall('list_rate_quotes', {'inquiry_id': inquiry_id})
        if inquiry_id and any(h in low for h in _SCHEDULE_HINTS) and 'list_schedule_options' not in called:
            return ToolCall('list_schedule_options', {'inquiry_id': inquiry_id})
        if 'lookup_sop' not in called:
            return ToolCall('lookup_sop', {'query': _clean_question(question)})
        return None


class ScriptedToolPolicy:
    """Replays a fixed script, then stops. For tests and for replaying a real trace."""

    name = 'scripted'

    def __init__(self, calls: list[ToolCall]) -> None:
        self._calls = list(calls)
        self._index = 0

    async def next_call(self, question: str, steps: list[ToolStep],
                        schema: list[dict]) -> ToolCall | None:
        if self._index >= len(self._calls):
            return None
        call = self._calls[self._index]
        self._index += 1
        return call


async def run_tool_loop(question: str, ctx: ToolContext, *,
                        policy: ToolPolicy | None = None,
                        tools: ToolRegistry | None = None,
                        max_steps: int | None = None) -> ToolLoopResult:
    s = get_settings()
    policy = policy or DeterministicToolPolicy()
    tools = tools or registry
    budget = max_steps or s.agent_max_tool_steps

    result = ToolLoopResult(question=question, answer='')
    if budget <= 0:
        result.stopped_reason = 'budget_exhausted'
        return result

    for _ in range(budget):
        call = await policy.next_call(question, result.steps, tools.schema())
        if call is None:
            result.stopped_reason = 'policy_done'
            break
        try:
            payload = await tools.invoke(call.name, ctx, call.arguments)
        except ToolError as exc:
            # A refused call is recorded and shown, never silently retried: retrying a
            # rejected tool is how a permission error turns into a loop.
            result.steps.append(ToolStep(call.name, call.arguments, False,
                                         error=f'{exc.code}:{exc.detail}'))
            continue
        result.steps.append(ToolStep(call.name, call.arguments, True, result=payload))
    else:
        result.stopped_reason = 'budget_exhausted'

    result.answer = compose_answer(result.steps)
    result.citations = _citations(result.steps)
    return result


def compose_answer(steps: list[ToolStep]) -> str:
    """Turn tool output into a user-facing sentence.

    Deliberately not an LLM call: the answer must be traceable to a tool result, and the
    refusal must be a refusal. A model asked to "summarise" an empty SOP result will
    produce something plausible, which is the failure this whole module exists to prevent.
    """
    ok = [s for s in steps if s.ok and s.result]
    if not ok:
        if steps and all(not s.ok for s in steps):
            return '无法完成查询：' + '; '.join(s.error or '' for s in steps)
        return ''

    for step in ok:
        payload = step.result or {}
        if step.name == 'lookup_sop':
            if payload.get('answered'):
                return payload.get('answer', '')
            return ('知识库中没有覆盖该问题的条文，无法给出有依据的答复。'
                    '建议转交操作主管确认，而不是依据通用经验回答。')

    parts: list[str] = []
    for step in ok:
        payload = step.result or {}
        if step.name == 'get_inquiry':
            parts.append(
                f"询价 {payload.get('inquiry_id')} 当前状态 {payload.get('status')}，"
                f"{payload.get('origin') or '?'} → {payload.get('destination') or '?'}，"
                f"箱型 {payload.get('equipment') or '?'}，ETD {payload.get('etd') or '?'}。"
                + (f"缺失字段：{', '.join(payload['missing_fields'])}。"
                   if payload.get('missing_fields') else '')
            )
        elif step.name == 'get_quote_status':
            if not payload.get('exists'):
                parts.append('该询价尚无客户报价单。')
            else:
                parts.append(
                    f"报价单 {payload.get('quote_id')} 状态 {payload.get('status')}，"
                    f"售价 {payload.get('sell_amount')} {payload.get('currency')}，"
                    f"有效期至 {payload.get('valid_until')}。"
                )
        elif step.name == 'list_rate_quotes':
            parts.append(f"已获取 {payload.get('count', 0)} 条运价快照。")
        elif step.name == 'list_schedule_options':
            parts.append(f"已获取 {payload.get('count', 0)} 个船期选项。")
    return ' '.join(p for p in parts if p)


def _citations(steps: list[ToolStep]) -> list[dict]:
    for step in steps:
        if step.ok and step.name == 'lookup_sop' and step.result:
            return list(step.result.get('citations') or [])
    return []


def trace_json(result: ToolLoopResult) -> str:
    """Compact rendering for logs and for feeding back to a model."""
    return json.dumps([s.as_dict() for s in result.steps], ensure_ascii=False, default=str)[:2000]


__all__ = [
    'DeterministicToolPolicy',
    'ScriptedToolPolicy',
    'ToolCall',
    'ToolLoopResult',
    'ToolPolicy',
    'ToolStep',
    'compose_answer',
    'run_tool_loop',
    'summarize',
    'trace_json',
]
