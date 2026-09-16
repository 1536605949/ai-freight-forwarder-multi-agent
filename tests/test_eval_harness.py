"""Proves the evaluation harness can actually fail.

An eval harness that cannot fail is worse than no harness: it manufactures confidence. The
previous version of `evaluation/run_eval.py` reported `pass=True` for three of its five
critical cases without executing anything, so the "critical eval: 0 fail" launch gate was a
rubber stamp.

The checkers are injectable (`run_case(client, case)`), which makes the harness itself
testable. `FakeClient` below stands in for the live API and can be told to be *broken* in
exactly the way each rule is meant to detect. If a checker stops detecting its own failure
mode, these tests fail.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evaluation'))

from run_eval import run_case  # noqa: E402


class FakeClient:
    """Stands in for the live API. Each flag breaks one specific guarantee."""

    def __init__(self, *, approve_gate_broken=False, margin_broken=False,
                 isolation_broken=False, pipeline_never_stops=False):
        self.approve_gate_broken = approve_gate_broken
        self.margin_broken = margin_broken
        self.isolation_broken = isolation_broken
        self.pipeline_never_stops = pipeline_never_stops
        self._n = 0

    def post(self, path, body, *, tenant=None):
        self._n += 1
        if path == '/api/v1/messages/inbound':
            text = str(body.get('body', ''))
            complete = '2026-' in text
            return 200, {'inquiry_id': f'inq_{self._n}', 'status': 'ready' if complete
                         else 'needs_clarification',
                         'missing_fields': [] if complete else ['etd']}
        if path == '/api/v1/orchestrate/run':
            cost = 2145.0
            margin = 0.0 if self.margin_broken else 257.4
            return 200, {
                'status': 'completed' if self.pipeline_never_stops else 'waiting_approval',
                'pending': {} if self.pipeline_never_stops else {'quote_id': f'cq_{self._n}'},
                'trace': [{'stage': 'optimize', 'status': 'ok', 'detail': '',
                           'outputs': {'cost_amount': cost, 'margin_amount': margin,
                                       'sell_amount': cost + margin}}],
            }
        if '/quotes/' in path and path.endswith('/send'):
            if self.approve_gate_broken:
                return 200, {'quote': {'status': 'sent'}, 'message_id': 'm1'}
            return 409, {'detail': 'approval_required'}
        raise AssertionError(f'unexpected POST {path}')

    def get(self, path, *, tenant=None):
        if path.startswith('/api/v1/inquiries/'):
            if tenant and tenant != 'tenant-demo' and not self.isolation_broken:
                return 404, {'detail': 'not found'}
            return 200, {'id': path.rsplit('/', 1)[-1], 'status': 'waiting_approval'}
        raise AssertionError(f'unexpected GET {path}')


CASES = {
    'parser': {'id': 'inq1', 'kind': 'parser', 'message': 'no date here',
               'expected_missing': ['etd'], 'critical': True},
    'hitl1': {'id': 'hitl1', 'kind': 'safety', 'rule': 'quote_send_requires_approval',
              'critical': True},
    'pricing1': {'id': 'pricing1', 'kind': 'safety',
                 'rule': 'sell_price_ge_cost_plus_minimum_margin',
                 'expect': {'min_margin_usd': 180.0}, 'critical': True},
    'tenant1': {'id': 'tenant1', 'kind': 'security', 'rule': 'tenant_resource_isolation',
                'expect': {'foreign_tenant': 'eval-probe-other'}, 'critical': True},
}


def test_healthy_service_passes_every_case():
    c = FakeClient()
    for case in CASES.values():
        result = run_case(c, case)
        assert result['pass'], f'{case["id"]} should pass: {result["detail"]}'
        assert result['executed']


# --------------------------------------------------------------------------- #
# Mutation tests: break one guarantee, the matching case must fail
# --------------------------------------------------------------------------- #

def test_detects_a_broken_approval_gate():
    result = run_case(FakeClient(approve_gate_broken=True), CASES['hitl1'])
    assert not result['pass']
    assert result['executed'], 'it ran, it just observed the wrong thing'
    assert '409' in result['detail']


def test_detects_a_margin_below_the_floor():
    result = run_case(FakeClient(margin_broken=True), CASES['pricing1'])
    assert not result['pass']
    assert 'below the 180.0 floor' in result['detail']


def test_detects_a_cross_tenant_leak():
    result = run_case(FakeClient(isolation_broken=True), CASES['tenant1'])
    assert not result['pass']
    assert 'cross-tenant read returned 200' in result['detail']


def test_detects_a_pipeline_that_never_stops():
    """If automation runs past the gate, the safety case must notice."""
    result = run_case(FakeClient(pipeline_never_stops=True), CASES['hitl1'])
    assert not result['pass']
    assert 'did not stop at the gate' in result['detail']


# --------------------------------------------------------------------------- #
# Fail-closed behaviour
# --------------------------------------------------------------------------- #

def test_unknown_kind_fails_and_is_not_counted_as_executed():
    result = run_case(FakeClient(), {'id': 'x', 'kind': 'astrology', 'critical': True})
    assert not result['pass']
    assert not result['executed'], 'a case that never ran must not be reported as a pass'
    assert 'unknown case kind' in result['detail']


def test_unknown_rule_fails():
    result = run_case(FakeClient(), {'id': 'x', 'kind': 'safety', 'rule': 'vibes',
                                     'critical': True})
    assert not result['pass'] and not result['executed']
    assert 'unknown rule' in result['detail']


def test_a_crashing_checker_is_a_failure_not_a_crash():
    class Exploding(FakeClient):
        def post(self, path, body, *, tenant=None):
            raise RuntimeError('boom')

    result = run_case(Exploding(), CASES['hitl1'])
    assert not result['pass'] and result['executed']
    assert 'RuntimeError' in result['detail']


@pytest.mark.parametrize('case_id', list(CASES))
def test_every_case_carries_an_executed_flag(case_id):
    """The gate reads this flag, so it must always be present and boolean."""
    result = run_case(FakeClient(), CASES[case_id])
    assert isinstance(result['executed'], bool)
