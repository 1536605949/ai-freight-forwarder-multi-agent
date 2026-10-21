"""Timeouts, bounded retries, and token/cost accounting for model calls.

Two production failures this module exists to prevent:

* **A hung provider stalls everything.** Without a timeout, one unresponsive model call
  blocks a worker slot indefinitely. A timeout turns that into a retryable failure.
* **A retry re-runs a business refusal.** Retrying is only correct for transient faults. A
  `ValueError` from a service means "this is not allowed", and repeating it burns quota to
  get the same answer. `retry_on` is therefore narrow and explicit, never `Exception`.

Token accounting is separate from retrying because the two answer different questions:
"did the call succeed" and "what did it cost". The extraction path is tested against both
the Microsoft Agent Framework shape and the OpenAI shape, so the numbers are real when a
provider is configured rather than a field that is always null.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence


class TransientError(Exception):
    """A failure that is worth retrying: network, 429, 5xx, provider timeout."""


async def call_with_retry(
    operation: Callable[[], Awaitable[Any]],
    *,
    timeout: float,
    retries: int,
    retry_on: Sequence[type[BaseException]] = (TransientError, asyncio.TimeoutError),
    backoff: float = 0.0,
    on_attempt: Callable[[int, BaseException | None], None] | None = None,
) -> Any:
    """Await `operation` with a per-attempt timeout and at most `retries` extra attempts.

    `retries` counts *additional* attempts, so retries=2 means at most 3 calls.
    """
    if retries < 0:
        raise ValueError('retries must be >= 0')

    last: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            result = await asyncio.wait_for(operation(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised unless explicitly retryable
            if not isinstance(exc, tuple(retry_on)):
                if on_attempt is not None:
                    on_attempt(attempt, exc)
                raise
            last = exc
            if on_attempt is not None:
                on_attempt(attempt, exc)
            if attempt < retries and backoff:
                await asyncio.sleep(backoff * (2 ** attempt))
            continue
        if on_attempt is not None:
            on_attempt(attempt, None)
        return result

    raise TransientError(f'exhausted {retries + 1} attempts') from last


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    @property
    def is_known(self) -> bool:
        return self.prompt_tokens is not None or self.completion_tokens is not None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def extract_usage(result: Any, *, model: str | None = None) -> Usage:
    """Read token counts from a provider result without assuming one SDK's shape.

    Returns a `Usage` with `None` fields when the provider reports nothing. That is the
    honest outcome: writing 0 would make "average cost per run" look excellent for the
    wrong reason, which is how a cost dashboard becomes decorative.
    """
    if result is None:
        return Usage(model=model)

    details = getattr(result, 'usage_details', None) or getattr(result, 'usage', None)
    if details is None and isinstance(result, dict):
        details = result.get('usage_details') or result.get('usage')
    if details is None:
        return Usage(model=model)

    def pick(*names: str) -> int | None:
        for name in names:
            value = details.get(name) if isinstance(details, dict) else getattr(details, name, None)
            if value is not None:
                return _as_int(value)
        return None

    resolved_model = model
    for attr in ('model', 'model_name', 'model_id'):
        candidate = getattr(result, attr, None)
        if isinstance(candidate, str) and candidate:
            resolved_model = candidate
            break

    return Usage(
        prompt_tokens=pick('input_token_count', 'prompt_tokens', 'input_tokens'),
        completion_tokens=pick('output_token_count', 'completion_tokens', 'output_tokens'),
        model=resolved_model,
    )


def estimate_cost(usage: Usage, *, prompt_price_per_1k: float,
                  completion_price_per_1k: float) -> float | None:
    """USD cost, or None when the provider reported no tokens."""
    if not usage.is_known:
        return None
    cost = ((usage.prompt_tokens or 0) / 1000.0) * prompt_price_per_1k
    cost += ((usage.completion_tokens or 0) / 1000.0) * completion_price_per_1k
    return round(cost, 6)
