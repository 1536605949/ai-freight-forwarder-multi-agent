"""Model-call discipline: timeouts, bounded retries, and token accounting.

The retry tests are the important ones. A retry loop that catches `Exception` looks
harmless and is not: it re-runs business refusals, multiplies side effects, and hides the
one failure the caller needed to see.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.agents.resilience import (TransientError, Usage, call_with_retry, estimate_cost,
                                   extract_usage)


class Counter:
    def __init__(self, fail_times: int, exc: BaseException | None = None) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.exc = exc or TransientError('boom')

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return 'ok'


@pytest.mark.asyncio
async def test_transient_failure_is_retried_and_succeeds():
    op = Counter(fail_times=2)
    assert await call_with_retry(op, timeout=1.0, retries=2) == 'ok'
    assert op.calls == 3


@pytest.mark.asyncio
async def test_retries_are_bounded():
    op = Counter(fail_times=10)
    with pytest.raises(TransientError):
        await call_with_retry(op, timeout=1.0, retries=2)
    assert op.calls == 3, 'retries=2 means at most three attempts'


@pytest.mark.asyncio
async def test_a_business_refusal_is_not_retried():
    """`ValueError` means "not allowed"; repeating it only burns quota."""
    op = Counter(fail_times=1, exc=ValueError('approval_required'))
    with pytest.raises(ValueError):
        await call_with_retry(op, timeout=1.0, retries=3)
    assert op.calls == 1


@pytest.mark.asyncio
async def test_a_hung_call_times_out_and_is_retried():
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.5)
        return 'ok'

    assert await call_with_retry(slow, timeout=0.05, retries=1) == 'ok'
    assert calls == 2


@pytest.mark.asyncio
async def test_attempts_are_observable():
    seen: list[tuple[int, str | None]] = []
    op = Counter(fail_times=1)
    await call_with_retry(op, timeout=1.0, retries=1,
                          on_attempt=lambda i, e: seen.append((i, type(e).__name__ if e else None)))
    assert seen == [(0, 'TransientError'), (1, None)]


@dataclass
class MafLikeResult:
    usage_details: dict
    model: str = 'gpt-4o-mini'


@dataclass
class OpenAILikeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class OpenAILikeResult:
    usage: OpenAILikeUsage


def test_extract_usage_from_agent_framework_shape():
    usage = extract_usage(MafLikeResult({'input_token_count': 120, 'output_token_count': 45}))
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 45
    assert usage.total_tokens == 165
    assert usage.model == 'gpt-4o-mini'


def test_extract_usage_from_openai_shape():
    usage = extract_usage(OpenAILikeResult(OpenAILikeUsage(10, 20)), model='gpt-4o')
    assert (usage.prompt_tokens, usage.completion_tokens) == (10, 20)
    assert usage.model == 'gpt-4o'


def test_absent_usage_stays_unknown_instead_of_zero():
    """A zero would make the cost dashboard look good for the wrong reason."""
    usage = extract_usage(object())
    assert usage.is_known is False
    assert usage.total_tokens is None
    assert estimate_cost(usage, prompt_price_per_1k=1.0, completion_price_per_1k=2.0) is None


def test_cost_is_computed_from_reported_tokens():
    usage = Usage(prompt_tokens=1000, completion_tokens=500, model='x')
    assert estimate_cost(usage, prompt_price_per_1k=0.15, completion_price_per_1k=0.60) == 0.45


def test_mock_runtime_reports_no_usage():
    """The mock runtime consumes no tokens; the record must say so, not claim zero cost."""
    usage = extract_usage(None)
    assert usage.is_known is False
