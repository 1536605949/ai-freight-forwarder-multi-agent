"""Node plumbing shared by the quote graph and the acquisition graph.

Both graphs need the same three things around every node: a trace entry that names the
owning agent, a conversion of business refusals into terminal states, and a timing sample
that includes the failure paths. Duplicating that in two files is how the second graph ends
up with a subtly different idea of what a failed node looks like.

`guarded` / `timed` are passed to `StateGraph.add_node`, so their signature is the graph's,
not ours.
"""
from __future__ import annotations

import inspect
import time

import app.harness as harness
from app.agents.registry import agent_for_node
from app.harness import get_action


def ms(started: float) -> int:
    """Whole milliseconds since `started`, floored at 0.

    Rounded rather than truncated so a fast node does not read as 0 ms purely from float
    noise; the point of the field is to be honest about a slow node, not to flatter a fast one.
    """
    return max(0, round((time.perf_counter() - started) * 1000))


def trace_step(node: str, status: str = 'ok', detail: str = '',
               elapsed_ms: int | None = None, action: str | None = None, **outputs) -> dict:
    """One trace entry: which node, which agent, and -- if it did something -- at what risk.

    The agent and the risk are derived, never passed in. A node cannot forget to declare its
    owner, and a step cannot claim a risk level different from the one the harness enforced.
    """
    step = {'node': node, 'agent': agent_for_node(node), 'status': status,
            'detail': detail, 'outputs': outputs}
    if action is not None:
        spec = get_action(action)
        if spec is None:
            # A step that performed an undeclared action would be a side effect outside the
            # policy table -- the one thing the harness exists to prevent.
            raise KeyError(f'undeclared_action: {action}')
        step['action'] = action
        step['risk'] = spec.risk.label
    if elapsed_ms is not None:
        step['elapsed_ms'] = elapsed_ms
    return step


def guarded(node: str, handler):
    """Run a node, converting a business refusal into a terminal state.

    A `ValueError` from a service is a *result* ("insufficient_supplier_quotes"), not a
    crash. Letting it propagate would abort the graph with a stack trace and lose the trace
    that explains which node refused.

    Every step produced here carries `elapsed_ms`. Timing lives in the wrapper rather than
    in each node so that adding a node cannot forget it, and so the failure paths are
    measured too -- a node that fails after 30 seconds is the one worth seeing.
    """

    async def wrapped(state) -> dict:
        started = time.perf_counter()
        try:
            result = handler(state)
            if inspect.isawaitable(result):
                result = await result
        except KeyError as exc:
            return {'status': 'failed', 'stop_reason': f'{node}:not_found:{exc}',
                    'steps': [trace_step(node, 'failed', f'not_found: {exc}',
                                         elapsed_ms=ms(started))]}
        except harness.HarnessError as exc:
            # A governance refusal is a business result, but it carries more than a string:
            # which rule fired, at what risk, for which agent. Keeping the decision in the
            # step is what lets an auditor answer "why was this blocked" from the trace
            # alone, without correlating the audit table.
            return {'status': 'failed', 'stop_reason': f'{node}:{exc}',
                    'steps': [trace_step(node, 'failed', str(exc), elapsed_ms=ms(started),
                                         harness=exc.decision.as_dict())]}
        except ValueError as exc:
            return {'status': 'failed', 'stop_reason': f'{node}:{exc}',
                    'steps': [trace_step(node, 'failed', str(exc), elapsed_ms=ms(started))]}

        elapsed = ms(started)
        for step in result.get('steps') or []:
            step.setdefault('elapsed_ms', elapsed)
        return result

    return wrapped


def timed(node: str, handler):
    """Measure a node that has no business refusal to catch.

    `clarify` only reshapes state, so it has nothing to guard -- but leaving it unmeasured
    would make it the one node that runs without a timing sample, and a metrics table that
    silently omits a node is worse than one that reports 0 ms for it.
    """

    async def wrapped(state) -> dict:
        started = time.perf_counter()
        result = handler(state)
        elapsed = ms(started)
        for step in result.get('steps') or []:
            step.setdefault('elapsed_ms', elapsed)
        return result

    return wrapped


__all__ = ['guarded', 'ms', 'timed', 'trace_step']
