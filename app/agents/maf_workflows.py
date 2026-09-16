"""Optional native Microsoft Agent Framework orchestration factories.

**NOT WIRED — reference implementation, deliberately unreachable from the running system.**
Nothing imports this module. It exists to show the intended shape of the real-LLM
orchestration path, and `tests/test_maf_workflows.py` pins that status so it cannot silently
rot into something that looks live but is not. See docs/ARCHITECTURE_REVIEW.md (P2-5).

Why the business app does not use these
---------------------------------------
The business application intentionally persists long-running state in PostgreSQL rather than
keeping one in-memory Agent Framework workflow alive for hours. These factories are for
bounded multi-agent subflows and create a fresh workflow per execution.

The split that matters: Agent Framework would own *understanding and drafting*. It would not
own pricing, approval, or sending — those stay in the deterministic service layer, and the
prompts below say so explicitly ("never alter input numbers").

Importing this module is safe without `agent_framework` installed: every framework import is
inside a function, so the optional dependency is only required when a factory is actually
called.
"""
from __future__ import annotations


def _require_agent_framework():
    """Import the optional dependency, failing with an actionable message if absent."""
    try:
        import agent_framework  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised via a monkeypatched import
        raise ImportError(
            'agent_framework is not installed. These workflows are an unwired reference '
            'implementation; install it with `pip install -r requirements-agent.txt` only if '
            'you intend to wire them up. Mock mode (AGENT_PROVIDER=mock) needs nothing.'
        ) from exc


def _client():
    _require_agent_framework()
    from agent_framework.openai import OpenAIChatClient
    from app.config import get_settings
    s=get_settings()
    return OpenAIChatClient(model=s.openai_chat_model) if s.openai_chat_model else OpenAIChatClient()


def build_inquiry_sequential_workflow():
    _require_agent_framework()
    from agent_framework import Agent
    from agent_framework.orchestrations import SequentialBuilder
    c=_client()
    parser=Agent(client=c,name='InquiryParser',instructions='Extract inquiry facts only. Do not invent missing data.')
    reviewer=Agent(client=c,name='InquiryQualityReviewer',instructions='Review extracted inquiry for ambiguity and list only missing required fields.')
    return SequentialBuilder(participants=[parser,reviewer],intermediate_output_from=[parser]).build()


def build_quote_concurrent_review_workflow():
    _require_agent_framework()
    from agent_framework import Agent
    from agent_framework.orchestrations import ConcurrentBuilder
    c=_client()
    price=Agent(client=c,name='PriceReviewer',instructions='Review relative supplier price competitiveness. Never alter input numbers.')
    transit=Agent(client=c,name='TransitReviewer',instructions='Review transit time, direct/transshipment and free time.')
    risk=Agent(client=c,name='ReliabilityReviewer',instructions='Review supplier reliability and validity risk using only supplied data.')
    return ConcurrentBuilder(participants=[price,transit,risk]).build()


def build_sales_handoff_workflow():
    _require_agent_framework()
    from agent_framework import Agent
    from agent_framework.orchestrations import HandoffBuilder
    c=_client()
    triage=Agent(client=c,name='SalesTriage',instructions='Route freight messages to inquiry, booking, tracking or sales follow-up specialist.',require_per_service_call_history_persistence=True)
    inquiry=Agent(client=c,name='InquirySpecialist',instructions='Handle ocean freight inquiry conversations.',require_per_service_call_history_persistence=True)
    booking=Agent(client=c,name='BookingSpecialist',instructions='Handle booking information collection; never claim confirmation without tool result.',require_per_service_call_history_persistence=True)
    tracking=Agent(client=c,name='TrackingSpecialist',instructions='Explain tracking events using only tool/provider events.',require_per_service_call_history_persistence=True)
    return (HandoffBuilder(name='freight_sales_handoff',participants=[triage,inquiry,booking,tracking])
            .with_start_agent(triage)
            .add_handoff(triage,[inquiry,booking,tracking])
            .add_handoff(inquiry,[triage]).add_handoff(booking,[triage]).add_handoff(tracking,[triage]).build())


WORKFLOW_FACTORIES = (
    'build_inquiry_sequential_workflow',
    'build_quote_concurrent_review_workflow',
    'build_sales_handoff_workflow',
)

__all__ = list(WORKFLOW_FACTORIES)

