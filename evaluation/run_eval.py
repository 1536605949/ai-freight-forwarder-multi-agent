"""Behavioural evaluation harness — runs the golden dataset against a live instance.

Run with the API up:
    python evaluation/run_eval.py                 # or: make eval
    python evaluation/run_eval.py --base-url http://localhost:8100

Exit codes: 0 = all critical cases passed, 1 = at least one critical case failed or was not
executed, 2 = the service was unreachable.

Design rules this harness follows
---------------------------------
* **A case that was not executed is not a pass.** Every result carries an `executed` flag and
  the process fails if a critical case failed *or* was never run. The previous version marked
  three of five critical cases `pass=True` with the detail "static rule documented" without
  executing anything, which made the "critical eval: 0 fail" launch gate a rubber stamp.
* **Every case must be able to fail.** Each checker asserts a property of live behaviour, so
  breaking the guard under test flips the case to FAIL. `tests/test_eval_harness.py` proves
  this by pointing the harness at a deliberately broken service.
* **An unknown `kind` fails loudly** instead of defaulting to success.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

import requests

HERE = Path(__file__).resolve().parent
DATASET = HERE / 'golden_dataset.jsonl'
REPORT = HERE / 'latest_report.json'

# A message that satisfies every REQUIRED inquiry field, so the pipeline can reach its gate.
COMPLETE_MESSAGE = ('Need 2x40HQ from Shanghai to Los Angeles, '
                    'ETD 2026-09-25, weight 18000kg, FOB')


class Unreachable(RuntimeError):
    """The service is not answering."""


class Client:
    """Thin HTTP wrapper that carries the demo identity headers."""

    def __init__(self, base_url: str, tenant: str = 'tenant-demo', user: str = 'eval') -> None:
        self.base_url = base_url.rstrip('/')
        self.tenant = tenant
        self.user = user

    def _headers(self, tenant: str | None) -> dict[str, str]:
        return {'X-Demo-User': self.user, 'X-Demo-Tenant': tenant or self.tenant}

    def post(self, path: str, body: dict, *, tenant: str | None = None) -> tuple[int, Any]:
        try:
            r = requests.post(self.base_url + path, headers=self._headers(tenant), json=body,
                              timeout=60)
        except requests.RequestException as exc:
            raise Unreachable(str(exc)) from exc
        return r.status_code, _body(r)

    def get(self, path: str, *, tenant: str | None = None) -> tuple[int, Any]:
        try:
            r = requests.get(self.base_url + path, headers=self._headers(tenant), timeout=60)
        except requests.RequestException as exc:
            raise Unreachable(str(exc)) from exc
        return r.status_code, _body(r)


def _body(r: requests.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


# --------------------------------------------------------------------------- #
# Fixtures the safety / security cases need
# --------------------------------------------------------------------------- #

def _inquiry_at_the_gate(c: Client, tag: str) -> dict[str, Any]:
    """Ingest a complete inquiry and run the pipeline up to the human gate.

    Uses only the public API, so the fixture itself is evidence that the documented
    ingestion path can actually produce a runnable inquiry.
    """
    status, body = c.post('/api/v1/messages/inbound', {
        'external_message_id': f'eval-{tag}-{uuid.uuid4().hex}',
        'sender_email': 'eval@example.com',
        'subject': 'quote request',
        'body': COMPLETE_MESSAGE,
    })
    if status != 200:
        raise AssertionError(f'inbound returned {status}: {body}')
    inquiry_id = body['inquiry_id']
    if body.get('missing_fields'):
        raise AssertionError(f'complete message still missing {body["missing_fields"]}')

    status, run = c.post('/api/v1/orchestrate/run', {'inquiry_id': inquiry_id})
    if status != 200:
        raise AssertionError(f'orchestrate returned {status}: {run}')
    return {'inquiry_id': inquiry_id, 'run': run}


def _stage_outputs(run: dict, stage: str) -> dict:
    for entry in run.get('trace', []):
        if entry.get('stage') == stage:
            return entry.get('outputs') or {}
    return {}


# --------------------------------------------------------------------------- #
# Checkers, one per `rule`
# --------------------------------------------------------------------------- #

def check_parser(c: Client, case: dict) -> tuple[bool, str, dict]:
    status, body = c.post('/api/v1/messages/inbound', {
        'external_message_id': f'eval-{case["id"]}-{uuid.uuid4().hex}',
        'sender_email': 'eval@example.com',
        'subject': 'quote request',
        'body': case['message'],
    })
    if status != 200:
        return False, f'inbound returned {status}', {'body': body}
    missing = sorted(body.get('missing_fields') or [])
    expected = sorted(case['expected_missing'])
    return missing == expected, f'missing={missing} expected={expected}', {'body': body}


def check_quote_send_requires_approval(c: Client, case: dict) -> tuple[bool, str, dict]:
    """The hard gate: an un-approved quote must not reach the customer."""
    ctx = _inquiry_at_the_gate(c, case['id'])
    run = ctx['run']
    quote_id = (run.get('pending') or {}).get('quote_id')
    if not quote_id:
        return False, 'pipeline did not stop at the gate with a quote to review', {'run': run}

    status, body = c.post(f'/api/v1/quotes/{quote_id}/send', {})
    detail = {'pipeline_status': run.get('status'), 'send_status': status, 'send_body': body}
    if run.get('status') != 'waiting_approval':
        return False, f'pipeline should be waiting_approval, was {run.get("status")}', detail
    if status != 409:
        return False, f'send of an un-approved quote returned {status}, expected 409', detail
    if 'approval_required' not in json.dumps(body):
        return False, f'refusal did not cite approval_required: {body}', detail
    return True, 'un-approved send refused with approval_required', detail


def check_sell_price_ge_cost_plus_minimum_margin(c: Client, case: dict) -> tuple[bool, str, dict]:
    """Money is computed by deterministic code, and it must cover cost + the floor margin."""
    ctx = _inquiry_at_the_gate(c, case['id'])
    out = _stage_outputs(ctx['run'], 'optimize')
    detail = {'optimize': out}
    if not out:
        return False, 'no optimize stage output to inspect', detail

    cost = float(out['cost_amount'])
    margin = float(out['margin_amount'])
    sell = float(out['sell_amount'])
    floor = float(case.get('expect', {}).get('min_margin_usd', 180.0))

    detail.update({'cost': cost, 'margin': margin, 'sell': sell, 'floor': floor})
    if abs(round(cost + margin, 2) - sell) > 0.01:
        return False, f'sell {sell} != cost {cost} + margin {margin}', detail
    if margin < floor - 1e-9:
        return False, f'margin {margin} is below the {floor} floor', detail
    if sell <= cost:
        return False, f'sell {sell} does not exceed cost {cost}', detail
    return True, f'sell {sell} = cost {cost} + margin {margin} (floor {floor})', detail


def check_tenant_resource_isolation(c: Client, case: dict) -> tuple[bool, str, dict]:
    """A resource must be readable by its owner and invisible to every other tenant."""
    ctx = _inquiry_at_the_gate(c, case['id'])
    inquiry_id = ctx['inquiry_id']
    foreign = case.get('expect', {}).get('foreign_tenant', 'eval-probe-other')

    own_status, _ = c.get(f'/api/v1/inquiries/{inquiry_id}')
    foreign_status, foreign_body = c.get(f'/api/v1/inquiries/{inquiry_id}', tenant=foreign)
    detail = {'inquiry_id': inquiry_id, 'owner_status': own_status,
              'foreign_tenant': foreign, 'foreign_status': foreign_status,
              'foreign_body': foreign_body}

    if own_status != 200:
        return False, f'owner could not read its own inquiry ({own_status})', detail
    if foreign_status != 404:
        return False, f'cross-tenant read returned {foreign_status}, expected 404', detail
    return True, 'owner 200, foreign tenant 404', detail


CHECKERS: dict[str, dict[str, Callable[[Client, dict], tuple[bool, str, dict]]]] = {
    'parser': {'default': check_parser},
    'safety': {
        'quote_send_requires_approval': check_quote_send_requires_approval,
        'sell_price_ge_cost_plus_minimum_margin': check_sell_price_ge_cost_plus_minimum_margin,
    },
    'security': {
        'tenant_resource_isolation': check_tenant_resource_isolation,
    },
}


def run_case(c: Client, case: dict) -> dict:
    """Execute one case. Anything unexpected is a failure, never a silent pass."""
    kind = case.get('kind', '')
    by_kind = CHECKERS.get(kind)
    if by_kind is None:
        return {**case, 'pass': False, 'executed': False,
                'detail': f'unknown case kind: {kind!r}'}

    checker = by_kind.get(case.get('rule') or 'default')
    if checker is None:
        return {**case, 'pass': False, 'executed': False,
                'detail': f'unknown rule for kind {kind!r}: {case.get("rule")!r}'}

    try:
        ok, detail, extra = checker(c, case)
    except AssertionError as exc:
        return {**case, 'pass': False, 'executed': True, 'detail': str(exc)}
    except Unreachable:
        raise
    except Exception as exc:  # a harness bug must surface as a failure, not a crash
        return {**case, 'pass': False, 'executed': True,
                'detail': f'{type(exc).__name__}: {exc}'}
    return {**case, 'pass': bool(ok), 'executed': True, 'detail': detail, 'evidence': extra}


def load_cases() -> list[dict]:
    lines = DATASET.read_text(encoding='utf-8').splitlines()
    return [json.loads(x) for x in lines if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Run the behavioural evaluation harness.')
    ap.add_argument('--base-url', default=os.getenv('BASE_URL', 'http://localhost:8100'))
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)

    c = Client(args.base_url)
    try:
        status, _ = c.get('/api/healthz')
    except Unreachable as exc:
        print(f'UNREACHABLE {args.base_url}: {exc}', file=sys.stderr)
        return 2
    if status != 200:
        print(f'UNREACHABLE {args.base_url}: healthz returned {status}', file=sys.stderr)
        return 2

    cases = load_cases()
    results = []
    for case in cases:
        r = run_case(c, case)
        results.append(r)
        if not args.quiet:
            flag = 'PASS' if r['pass'] else ('SKIP' if not r['executed'] else 'FAIL')
            print(f'{flag} {r["id"]:<10} {r["detail"]}')

    passed = sum(1 for r in results if r['pass'])
    critical = [r for r in results if r.get('critical')]
    critical_failed = [r for r in critical if not r['pass']]
    critical_unexecuted = [r for r in critical if not r['executed']]

    report = {
        'base_url': args.base_url,
        'passed': passed,
        'total': len(results),
        'critical_total': len(critical),
        'critical_failed': [r['id'] for r in critical_failed],
        'critical_unexecuted': [r['id'] for r in critical_unexecuted],
        'cases': results,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                      encoding='utf-8')

    print(f'\n{passed}/{len(results)} passed; '
          f'critical: {len(critical) - len(critical_failed) - len(critical_unexecuted)}/'
          f'{len(critical)} ok, {len(critical_failed)} failed, '
          f'{len(critical_unexecuted)} not executed')
    if critical_failed or critical_unexecuted:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
