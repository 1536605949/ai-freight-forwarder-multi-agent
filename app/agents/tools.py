"""Read-only tool registry exposed to the model.

The registry is the boundary between "the model may look things up" and "the model may
change things". Every tool here is `read_only=True`, tenant-scoped, and cannot price,
approve, send, or book. `invoke()` refuses anything that is not read-only *even if such a
tool is registered later* -- the guard is in the dispatch path, not in a comment, so a
future write tool cannot be reached by a model without someone deliberately changing this
function and its test.

Why a registry rather than ad-hoc function calls
------------------------------------------------
The model needs a schema to choose from, and the system needs one place to enforce
permission, argument validation, audit, and error shaping. Scattering `if intent == ...`
across an agent loop gives you the schema but not the guarantees.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from app.knowledge import answer_question
from app.models import CustomerQuote, Inquiry, RateSnapshot, ScheduleOption
from app.services.common import audit


class ToolError(Exception):
    """A refused or failed tool call. Returned to the model, never raised past the loop."""

    def __init__(self, code: str, detail: str = '') -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ToolContext:
    db: Session
    tenant_id: str
    actor: str
    trace_id: str = ''


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], Awaitable[dict] | dict]
    read_only: bool = True


def _require(args: dict, *names: str) -> None:
    missing = [n for n in names if not args.get(n)]
    if missing:
        raise ToolError('invalid_arguments', f'missing: {",".join(missing)}')


async def _lookup_sop(ctx: ToolContext, args: dict) -> dict:
    _require(args, 'query')
    result = answer_question(ctx.db, ctx.tenant_id, str(args['query']))
    payload = result.as_dict()
    if not payload['answered']:
        payload['hint'] = (
            'No SOP passage scored above the evidence threshold. Tell the user the '
            'knowledge base does not cover this; do not answer from general knowledge.'
        )
    return payload


def _get_inquiry(ctx: ToolContext, args: dict) -> dict:
    _require(args, 'inquiry_id')
    row = (ctx.db.query(Inquiry)
           .filter_by(id=str(args['inquiry_id']), tenant_id=ctx.tenant_id).first())
    if not row:
        raise ToolError('not_found', 'inquiry does not exist in this tenant')
    return {
        'inquiry_id': row.id, 'status': row.status,
        'origin': row.origin, 'destination': row.destination,
        'equipment': row.equipment, 'quantity': row.quantity, 'etd': row.etd,
        'commodity': row.commodity, 'incoterm': row.incoterm,
        'missing_fields': list(row.missing_fields or []),
    }


def _list_schedule_options(ctx: ToolContext, args: dict) -> dict:
    _require(args, 'inquiry_id')
    rows = (ctx.db.query(ScheduleOption)
            .filter_by(tenant_id=ctx.tenant_id, inquiry_id=str(args['inquiry_id']))
            .order_by(ScheduleOption.transit_days).limit(10).all())
    return {'count': len(rows), 'options': [
        {'carrier': r.carrier, 'service': r.service, 'vessel': r.vessel,
         'voyage': r.voyage, 'etd': r.etd, 'eta': r.eta,
         'transit_days': r.transit_days, 'direct': r.direct,
         'source': r.source}
        for r in rows
    ]}


def _list_rate_quotes(ctx: ToolContext, args: dict) -> dict:
    _require(args, 'inquiry_id')
    rows = (ctx.db.query(RateSnapshot)
            .filter_by(tenant_id=ctx.tenant_id, inquiry_id=str(args['inquiry_id']))
            .order_by(RateSnapshot.total_cost).limit(10).all())
    return {'count': len(rows), 'quotes': [
        {'carrier': r.carrier, 'currency': r.currency, 'base_rate': r.base_rate,
         'surcharges': r.surcharges, 'total_cost': r.total_cost,
         'validity': r.validity, 'source': r.source}
        for r in rows
    ]}


def _get_quote_status(ctx: ToolContext, args: dict) -> dict:
    _require(args, 'inquiry_id')
    row = (ctx.db.query(CustomerQuote)
           .filter_by(tenant_id=ctx.tenant_id, inquiry_id=str(args['inquiry_id']))
           .order_by(CustomerQuote.created_at.desc()).first())
    if not row:
        return {'exists': False}
    return {
        'exists': True, 'quote_id': row.id, 'status': row.status,
        'currency': row.currency, 'cost_amount': row.cost_amount,
        'margin_amount': row.margin_amount, 'sell_amount': row.sell_amount,
        'valid_until': row.valid_until, 'approved_by': row.approved_by,
    }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name='lookup_sop',
        description=(
            'Search the company standard-operating-procedure knowledge base and return '
            'cited passages. Use for any "how do we handle X" question. If it reports '
            'answered=false, the knowledge base does not cover the question.'
        ),
        parameters={
            'type': 'object',
            'properties': {'query': {'type': 'string', 'description': 'The user question, verbatim.'}},
            'required': ['query'],
        },
        handler=_lookup_sop,
    ),
    ToolSpec(
        name='get_inquiry',
        description='Read one inquiry (origin, destination, equipment, ETD, missing fields).',
        parameters={
            'type': 'object',
            'properties': {'inquiry_id': {'type': 'string'}},
            'required': ['inquiry_id'],
        },
        handler=_get_inquiry,
    ),
    ToolSpec(
        name='list_schedule_options',
        description='List sailing options already retrieved for an inquiry.',
        parameters={
            'type': 'object',
            'properties': {'inquiry_id': {'type': 'string'}},
            'required': ['inquiry_id'],
        },
        handler=_list_schedule_options,
    ),
    ToolSpec(
        name='list_rate_quotes',
        description='List market/contract rate snapshots already retrieved for an inquiry.',
        parameters={
            'type': 'object',
            'properties': {'inquiry_id': {'type': 'string'}},
            'required': ['inquiry_id'],
        },
        handler=_list_rate_quotes,
    ),
    ToolSpec(
        name='get_quote_status',
        description='Read the current customer quote for an inquiry, including its status.',
        parameters={
            'type': 'object',
            'properties': {'inquiry_id': {'type': 'string'}},
            'required': ['inquiry_id'],
        },
        handler=_get_quote_status,
    ),
)


class ToolRegistry:
    def __init__(self, specs: tuple[ToolSpec, ...] = TOOL_SPECS) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def names(self) -> list[str]:
        return sorted(self._specs)

    def get(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolError('unknown_tool', f'no tool named {name!r}; available: {self.names()}')
        return spec

    def schema(self) -> list[dict]:
        """OpenAI-compatible tool declarations, so the same registry works with any model."""
        return [
            {'type': 'function',
             'function': {'name': s.name, 'description': s.description, 'parameters': s.parameters}}
            for s in self._specs.values()
        ]

    async def invoke(self, name: str, ctx: ToolContext, arguments: dict | str | None) -> dict:
        spec = self.get(name)
        if not spec.read_only:
            # A write tool must never be reachable by model choice alone.
            raise ToolError('tool_not_permitted', f'{name} is not read-only')

        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError as exc:
                raise ToolError('invalid_arguments', 'arguments are not valid JSON') from exc
        args = args or {}
        if not isinstance(args, dict):
            raise ToolError('invalid_arguments', 'arguments must be an object')

        try:
            result = spec.handler(ctx, args)
            if hasattr(result, '__await__'):
                result = await result
        except ToolError:
            raise
        except KeyError as exc:
            raise ToolError('not_found', str(exc)) from exc
        except ValueError as exc:
            raise ToolError('invalid_arguments', str(exc)) from exc
        except Exception as exc:  # a tool failure must not take the conversation down
            raise ToolError('tool_failed', f'{type(exc).__name__}: {exc}') from exc

        audit(ctx.db, ctx.tenant_id, ctx.actor, 'tool.invoked', 'tool', name,
              {'arguments': args, 'ok': True})
        ctx.db.commit()
        return result


registry = ToolRegistry()


def tool_names() -> list[str]:
    return registry.names()


def summarize(result: Any, limit: int = 400) -> str:
    """Compact a tool result for the prompt, so a big payload cannot eat the context."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + '…'
