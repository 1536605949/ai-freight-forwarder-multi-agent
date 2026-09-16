"""Rate provider invariants.

Rates feed the pricing engine, which decides what a customer is charged. Two properties
matter most and both are easy to break silently:

1. **Determinism** - the same lane/equipment must always produce the same numbers, or the
   demo flickers and no test can assert on prices.
2. **Provenance** - a synthetic demo rate must be visibly distinct from a contracted one.
   If the mock ever emitted `source='contract'`, a demo price could be mistaken for a real
   negotiated rate in an audit.
"""
import pytest

from app.providers.rates import (
    ContractRateProvider,
    IndexRateProvider,
    MockRateProvider,
    RateResult,
    Surcharge,
    get_rate_provider,
    _normalize_rate,
)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_mock_rates_are_deterministic():
    p = MockRateProvider()
    a = await p.search('Shanghai', 'Los Angeles', '40HQ', '2026-09-20')
    b = await p.search('Shanghai', 'Los Angeles', '40HQ', '2026-09-20')
    assert [(x.carrier, x.base_rate, x.surcharges) for x in a] == \
           [(x.carrier, x.base_rate, x.surcharges) for x in b]


@pytest.mark.asyncio
async def test_different_lanes_produce_different_rates():
    p = MockRateProvider()
    sh_la = await p.search('Shanghai', 'Los Angeles', '40HQ')
    sh_rt = await p.search('Shanghai', 'Rotterdam', '40HQ')
    assert sh_la[0].base_rate != sh_rt[0].base_rate, 'lane must affect price'


@pytest.mark.asyncio
async def test_equipment_changes_price():
    p = MockRateProvider()
    twenty = await p.search('Shanghai', 'Los Angeles', '20GP')
    forty = await p.search('Shanghai', 'Los Angeles', '40HQ')
    assert twenty[0].base_rate < forty[0].base_rate


@pytest.mark.asyncio
async def test_totals_reconcile_with_breakdown():
    """itemized surcharges must add up to the scalar `surcharges` field."""
    for r in await MockRateProvider().search('Shanghai', 'Los Angeles', '40HQ'):
        assert round(sum(s.amount for s in r.surcharge_breakdown), 2) == r.surcharges
        assert r.total_cost == round(r.base_rate + r.surcharges, 2)
        assert r.all_in == r.total_cost


@pytest.mark.asyncio
async def test_rate_result_shape_is_stable_for_downstream():
    """The 2-result default is load-bearing: pricing compares options."""
    rows = await MockRateProvider().search('Shanghai', 'Los Angeles', '40HQ')
    assert len(rows) == 2
    assert all(isinstance(r, RateResult) for r in rows)
    assert all(r.total_cost > 0 and r.currency == 'USD' for r in rows)


# --------------------------------------------------------------------------- #
# Provenance and validity
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_mock_rates_are_self_identifying_as_synthetic():
    for r in await MockRateProvider().search('Shanghai', 'Los Angeles', '40HQ'):
        assert r.source.startswith('demo_'), 'synthetic rates must not look contracted'
        assert (r.payload or {}).get('synthetic') is True


@pytest.mark.asyncio
async def test_mock_rates_carry_a_validity_window():
    for r in await MockRateProvider().search('Shanghai', 'Los Angeles', '40HQ'):
        assert r.validity and r.valid_until
        assert not r.is_expired(), 'freshly issued rates must not be expired'


def test_expiry_is_conservative_when_validity_is_unknown():
    """An unknown validity must not be treated as expired - that would block quoting."""
    r = RateResult('contract', 'MAEU', 'USD', 2000, 100)
    assert r.valid_until is None
    assert r.is_expired() is False


def test_expiry_detects_past_dates():
    r = RateResult('contract', 'MAEU', 'USD', 2000, 100, valid_until='2020-01-01')
    assert r.is_expired('2026-09-12') is True
    assert r.is_expired('2019-01-01') is False


# --------------------------------------------------------------------------- #
# Vendor normalization (the real adapters share this path)
# --------------------------------------------------------------------------- #

def test_normalize_flattens_vendor_payload():
    vendor = {
        'carrierCode': 'MAEU', 'currency': 'USD', 'oceanFreight': '2,150.00',
        'chargeItems': [
            {'chargeCode': 'BAF', 'name': 'Bunker', 'value': 129.0},
            {'chargeCode': 'THC', 'label': 'Terminal', 'amount': 140.0},
        ],
        'transitDays': '13', 'freeDays': 7, 'validUntil': '2026-10-31',
    }
    r = _normalize_rate(vendor, 'Shanghai', 'Los Angeles', '40HQ', source='contract')
    assert r.base_rate == 2150.0, 'comma/currency-formatted strings must parse'
    assert r.carrier == 'MAEU'
    assert r.surcharges == 269.0
    assert r.total_cost == 2419.0
    assert r.transit_days == 13 and r.free_days == 7
    assert r.source == 'contract'
    assert {s.code for s in r.surcharge_breakdown} == {'BAF', 'THC'}


def test_normalize_accepts_dict_shaped_surcharges():
    vendor = {'baseRate': 1000, 'surcharges': {'BAF': 60, 'THC': 140}}
    r = _normalize_rate(vendor, 'A', 'B', '20GP', source='contract')
    assert r.surcharges == 200.0
    assert len(r.surcharge_breakdown) == 2


def test_normalize_prefers_explicit_surcharge_total():
    vendor = {'baseRate': 1000, 'surchargesTotal': 250, 'chargeItems': [{'code': 'BAF', 'amount': 60}]}
    r = _normalize_rate(vendor, 'A', 'B', '20GP', source='index')
    assert r.surcharges == 250.0


def test_normalize_survives_a_sparse_payload():
    """A vendor returning almost nothing must not crash the quote pipeline."""
    r = _normalize_rate({}, 'A', 'B', '40HQ', source='index')
    assert r.base_rate == 0.0 and r.surcharges == 0.0
    assert r.currency == 'USD' and r.source == 'index'


def test_surcharge_defaults_are_sane():
    s = Surcharge('BAF', 'Bunker', 10.0)
    assert s.basis == 'per_container'


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #

def test_default_provider_is_mock():
    assert isinstance(get_rate_provider(), MockRateProvider)


@pytest.mark.asyncio
@pytest.mark.parametrize('cls,env', [
    (ContractRateProvider, 'RATE_CONTRACT_BASE_URL'),
    (IndexRateProvider, 'RATE_INDEX_BASE_URL'),
])
async def test_real_provider_skeletons_fail_loud_when_unconfigured(cls, env, monkeypatch):
    """An unconfigured real provider must raise, never silently return empty rates."""
    monkeypatch.delenv(env, raising=False)
    p = cls()
    p.base = ''
    with pytest.raises(RuntimeError, match=env):
        await p.search('Shanghai', 'Los Angeles', '40HQ')
