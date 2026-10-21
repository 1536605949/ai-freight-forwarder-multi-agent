"""Tool registry and the bounded tool-calling loop.

The guarantees under test are the ones that make model-driven tool use safe:

* the model can only reach read-only tools, enforced in the dispatch path;
* every tool is tenant-scoped, so a guessed id returns "not found" rather than another
  tenant's data;
* the loop has a hard step budget, and a refused call is recorded rather than retried;
* when the SOP lookup finds nothing, the composed answer refuses instead of improvising.
"""
from __future__ import annotations

import pytest

from app.agents.tool_loop import (DeterministicToolPolicy, ScriptedToolPolicy, ToolCall,
                                  compose_answer, run_tool_loop)
from app.agents.tools import ToolContext, ToolError, ToolRegistry, ToolSpec, registry
from app.knowledge.service import seed_corpus
from app.models import Customer, Inquiry

TENANT = 'tenant-demo'


@pytest.fixture
def ctx(db):
    return ToolContext(db=db, tenant_id=TENANT, actor='tester', trace_id='t1')


def _seed_inquiry(db, inquiry_id: str = 'inq_tool', tenant_id: str = TENANT) -> None:
    db.add(Customer(id=f'cus_{inquiry_id}', tenant_id=tenant_id, company='Acme',
                    email=f'{inquiry_id}@acme-import.com'))
    db.add(Inquiry(id=inquiry_id, tenant_id=tenant_id, customer_id=f'cus_{inquiry_id}',
                   raw_message='x', origin='Shanghai', destination='Los Angeles',
                   equipment='40HQ', quantity=1, etd='2026-09-25',
                   missing_fields=[], status='ready', external_message_id=f'm-{inquiry_id}'))
    db.commit()


def test_schema_is_openai_compatible_and_covers_every_tool():
    schema = registry.schema()
    assert {s['function']['name'] for s in schema} == set(registry.names())
    for entry in schema:
        assert entry['type'] == 'function'
        assert entry['function']['parameters']['type'] == 'object'
        assert entry['function']['description']


def test_every_shipped_tool_is_read_only():
    """If this ever fails, a model can mutate business state by choosing a tool."""
    assert all(spec.read_only for spec in registry._specs.values())


def test_unknown_tool_is_refused(ctx):
    import asyncio
    with pytest.raises(ToolError) as exc:
        asyncio.run(registry.invoke('send_everything', ctx, {}))
    assert exc.value.code == 'unknown_tool'


def test_a_write_tool_cannot_be_invoked_even_if_registered(ctx):
    """The guard lives in dispatch, not in a comment, so it holds for future tools too."""
    import asyncio

    def handler(_ctx, _args):
        raise AssertionError('must never run')

    write_tool = ToolSpec(name='approve_quote', description='writes',
                          parameters={'type': 'object', 'properties': {}},
                          handler=handler, read_only=False)
    guarded = ToolRegistry((write_tool,))

    with pytest.raises(ToolError) as exc:
        asyncio.run(guarded.invoke('approve_quote', ctx, {'quote_id': 'cq_1'}))
    assert exc.value.code == 'tool_not_permitted'


def test_invalid_arguments_are_reported_not_crashed(ctx):
    import asyncio
    with pytest.raises(ToolError) as exc:
        asyncio.run(registry.invoke('get_inquiry', ctx, {}))
    assert exc.value.code == 'invalid_arguments'


def test_tools_are_tenant_scoped(ctx, db):
    import asyncio
    _seed_inquiry(db, 'inq_secret', tenant_id='tenant-other')
    with pytest.raises(ToolError) as exc:
        asyncio.run(registry.invoke('get_inquiry', ctx, {'inquiry_id': 'inq_secret'}))
    assert exc.value.code == 'not_found'


@pytest.mark.asyncio
async def test_lookup_sop_returns_a_refusal_for_an_uncovered_question(ctx, db):
    seed_corpus(db, TENANT)
    payload = await registry.invoke('lookup_sop', ctx, {'query': '公司食堂几点开饭'})
    assert payload['answered'] is False
    assert payload['reason'] == 'insufficient_evidence'
    assert 'hint' in payload


@pytest.mark.asyncio
async def test_lookup_sop_answers_with_citations(ctx, db):
    seed_corpus(db, TENANT)
    payload = await registry.invoke('lookup_sop', ctx, {'query': '甩柜了怎么处理'})
    assert payload['answered'] is True
    assert payload['citations']


@pytest.mark.asyncio
async def test_deterministic_policy_reads_the_inquiry_then_the_sop(ctx, db):
    seed_corpus(db, TENANT)
    # Real ids are `inq_` + uuid hex; the policy only trusts that shape.
    _seed_inquiry(db, 'inq_abc123def456')
    result = await run_tool_loop('inq_abc123def456 这票货的订舱流程是什么', ctx)

    assert [s.name for s in result.steps] == ['get_inquiry', 'lookup_sop']
    assert result.stopped_reason == 'policy_done'
    assert result.answer
    assert result.citations


@pytest.mark.asyncio
async def test_step_budget_is_enforced(ctx):
    calls = [ToolCall('get_inquiry', {'inquiry_id': f'inq_{i}'}) for i in range(6)]
    result = await run_tool_loop('x', ctx, policy=ScriptedToolPolicy(calls), max_steps=2)

    assert result.stopped_reason == 'budget_exhausted'
    assert len(result.steps) == 2


@pytest.mark.asyncio
async def test_a_refused_call_is_recorded_and_does_not_abort_the_loop(ctx, db):
    seed_corpus(db, TENANT)
    calls = [ToolCall('send_everything', {}), ToolCall('lookup_sop', {'query': '甩柜了怎么处理'})]
    result = await run_tool_loop('x', ctx, policy=ScriptedToolPolicy(calls))

    assert result.steps[0].ok is False
    assert 'unknown_tool' in (result.steps[0].error or '')
    assert result.steps[1].ok is True
    assert result.answer


@pytest.mark.asyncio
async def test_ask_with_no_inquiry_id_still_answers_procedural_questions(ctx, db):
    seed_corpus(db, TENANT)
    result = await run_tool_loop('滞箱费怎么算', ctx)
    assert [s.name for s in result.steps] == ['lookup_sop']
    assert result.answer


def test_compose_answer_refuses_when_the_sop_lookup_found_nothing():
    from app.agents.tool_loop import ToolStep

    steps = [ToolStep('lookup_sop', {'query': 'x'}, True,
                      result={'answered': False, 'reason': 'insufficient_evidence'})]
    answer = compose_answer(steps)
    assert '没有覆盖' in answer
    assert '建议' in answer


def test_compose_answer_reports_a_failed_lookup():
    from app.agents.tool_loop import ToolStep

    steps = [ToolStep('lookup_sop', {'query': 'x'}, False, error='tool_failed:boom')]
    assert '无法完成查询' in compose_answer(steps)


def test_deterministic_policy_does_not_invent_an_inquiry_id(ctx):
    """No id in the text means no business lookup: the policy must not guess one."""
    import asyncio

    policy = DeterministicToolPolicy()
    call = asyncio.run(policy.next_call('这票货到哪了', [], registry.schema()))
    assert call is not None
    assert call.name == 'lookup_sop'
