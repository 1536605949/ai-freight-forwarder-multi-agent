"""Rate (pricing) data sources.

A `RateProvider` answers: "what does it cost to move this box on this lane?"

Design notes
------------
* **Rates are *cost* inputs, not sell prices.** Nothing here produces a customer-facing
  number. `PricingEngine` applies margin and minimum-margin rules downstream. A provider
  that returned a sell price would let a data vendor silently decide the company's margin.
* **`base_rate` + `surcharges` stay as the two canonical money fields.** The pricing engine
  and `RateSnapshot` only understand those two, so richer vendor payloads are *flattened*
  into them at the boundary rather than leaking vendor shapes inward. Everything a vendor
  gives us that does not fit survives in `surcharge_breakdown` / `payload`.
* **Every result records its `source`.** A contract rate, an index rate and a synthetic
  demo rate are not interchangeable when you are explaining a quote to a customer, so the
  provenance travels with the number.
* **Mock output is deterministic.** Same lane + equipment -> same rates, always. A demo
  that shows different prices on each refresh looks broken, and a test cannot assert on it.

Production note: contracted rates live in the carrier/forwarder agreement and index rates
come from a paid subscription (e.g. a spot-index vendor). Both are licensed commercial data;
the skeletons below do not hard-code any vendor URL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib

import httpx

from app.config import get_settings


@dataclass
class Surcharge:
    """One itemized cost component. Kept separate so a quote can explain its own price."""
    code: str
    label: str
    amount: float
    basis: str = 'per_container'  # per_container | per_shipment | per_bill


@dataclass
class RateResult:
    source: str
    carrier: str | None
    currency: str
    base_rate: float
    surcharges: float
    validity: str | None = None
    payload: dict | None = None
    # --- richer fields, additive only (existing callers keep working) ---
    equipment: str | None = None
    origin: str | None = None
    destination: str | None = None
    service: str | None = None
    transit_days: int | None = None
    free_days: int | None = None
    min_volume: float | None = None
    # Itemized breakdown. Sum of items should reconcile with `surcharges`.
    surcharge_breakdown: list[Surcharge] = field(default_factory=list)
    # Rates expire. A stale rate must not be used to build a quote without a refresh.
    valid_until: str | None = None

    @property
    def total_cost(self) -> float:
        return round(self.base_rate + self.surcharges, 2)

    @property
    def all_in(self) -> float:
        return self.total_cost

    def is_expired(self, on: str | None = None) -> bool:
        """True when the rate's validity window has closed. Unknown validity => not expired."""
        if not self.valid_until:
            return False
        ref = on or date.today().isoformat()
        return self.valid_until < ref


class RateProvider:
    async def search(self, origin: str, destination: str, equipment: str, etd: str | None = None) -> list[RateResult]:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Mock - deterministic synthetic rates
# --------------------------------------------------------------------------- #

# Coarse lane pricing so the demo produces plausible, *stable* relative differences:
# a trans-Pacific box costs more than an intra-Asia box, and that ordering never flips.
_EQUIPMENT_BASE = {'20GP': 1450.0, '40GP': 2050.0, '40HQ': 2150.0, '40RF': 3900.0, '45HQ': 2450.0, 'LCL': 62.0}
_CARRIERS = [
    # (SCAC, service code, rate multiplier, transit days, direct, free days)
    ('MAEU', 'TP1', 1.00, 13, True, 7),
    ('CMDU', 'PEX', 0.96, 15, True, 5),
    ('HLCU', 'PSX', 0.93, 18, False, 4),
    ('ONEY', 'AEX', 0.98, 16, True, 6),
    ('EGLV', 'NEX', 0.94, 21, False, 3),
]


def _lane_factor(origin: str, destination: str) -> float:
    """Deterministic multiplier in ~[0.85, 1.45] derived from the lane strings."""
    h = hashlib.sha256(f'{origin}|{destination}'.lower().encode('utf-8')).hexdigest()
    return round(0.85 + (int(h[:8], 16) % 6000) / 10000, 4)


def _lane_region_pair(origin: str, destination: str) -> tuple[str, str]:
    """Very small region classifier, enough to order lanes sensibly in a demo."""
    def region(s: str) -> str:
        t = (s or '').lower()
        if any(k in t for k in ('shanghai', 'ningbo', 'shenzhen', 'qingdao', 'xiamen', 'tianjin', 'china')):
            return 'CN'
        if any(k in t for k in ('los angeles', 'long beach', 'new york', 'savannah', 'houston', 'seattle')):
            return 'US'
        if any(k in t for k in ('rotterdam', 'hamburg', 'antwerp', 'felixstowe', 'le havre')):
            return 'EU'
        if any(k in t for k in ('singapore', 'laem chabang', 'ho chi minh', 'jakarta', 'manila')):
            return 'SEA'
        return 'XX'
    return region(origin), region(destination)


_MULTI_HOP = {('CN', 'US'), ('CN', 'EU'), ('SEA', 'US'), ('SEA', 'EU')}


class MockRateProvider:
    """Deterministic synthetic rates for demo and tests. Never present as live pricing.

    The generated prices are plausible in shape (lane ordering, equipment spread, carrier
    spread) but are entirely synthetic. Every result carries `payload['synthetic'] = True`
    and `source` starting with `demo_`, so a synthetic number can never be mistaken for a
    contracted one in an audit.
    """

    def __init__(self, carriers: int = 2) -> None:
        # Two results by default: the pricing comparison needs at least two options to
        # be meaningful, and downstream tests assert on exactly two.
        self.carriers = max(1, carriers)

    async def search(self, origin: str, destination: str, equipment: str = '40HQ', etd: str | None = None):
        base = _EQUIPMENT_BASE.get((equipment or '40HQ').upper(), 2150.0)
        factor = _lane_factor(origin, destination)
        r_origin, r_dest = _lane_region_pair(origin, destination)
        # Long-haul lanes carry a premium; short-haul a discount. Deterministic, not random.
        if (r_origin, r_dest) in _MULTI_HOP:
            factor *= 1.18
        elif r_origin == r_dest and r_origin != 'XX':
            factor *= 0.55
        # Slight seasonality so an ETD in peak season costs more, still fully deterministic.
        if etd:
            try:
                month = int(str(etd)[5:7])
                if month in (8, 9, 10):  # pre-peak / golden week build-up
                    factor *= 1.07
            except (ValueError, TypeError):
                pass

        valid_until = (date.today() + timedelta(days=30)).isoformat()
        out: list[RateResult] = []
        for i, (scac, service, mult, transit, direct, free_days) in enumerate(_CARRIERS[: self.carriers]):
            ocean = round(base * factor * mult, 2)
            # Surcharges modelled as the components a forwarder actually gets billed for.
            baf = round(ocean * 0.06, 2)                       # bunker adjustment
            thc = round(140.0 if '40' in equipment.upper() else 105.0, 2)
            docs = 45.0
            items = [
                Surcharge('BAF', 'Bunker Adjustment Factor', baf),
                Surcharge('THC', 'Terminal Handling Charge', thc),
                Surcharge('DOC', 'Documentation Fee', docs, basis='per_shipment'),
            ]
            surcharges = round(sum(s.amount for s in items), 2)
            out.append(RateResult(
                source='demo_contract' if i == 0 else 'demo_historical',
                carrier=scac, currency='USD',
                base_rate=ocean, surcharges=surcharges,
                validity=valid_until,
                equipment=equipment, origin=origin, destination=destination,
                service=service, transit_days=transit, free_days=free_days,
                min_volume=1.0,
                surcharge_breakdown=items, valid_until=valid_until,
                payload={
                    'synthetic': True,
                    'direct': direct,
                    'lane_factor': factor,
                    'note': 'Synthetic demo rate. Not a carrier quotation.',
                },
            ))
        return out


# --------------------------------------------------------------------------- #
# Contract rates - skeleton
# --------------------------------------------------------------------------- #

class ContractRateProvider:
    """Skeleton for contracted/negotiated rate lookup (carrier API or forwarder tariff file).

    Intentionally not implemented: the endpoint, auth model and field mapping are
    contract-specific. Normalize the vendor payload into `RateResult` *here*, flattening
    any vendor-specific surcharge structure into `surcharge_breakdown` + `base_rate` +
    `surcharges`, so the pricing engine never sees vendor shapes.
    """
    def __init__(self) -> None:
        s = get_settings()
        self.base = (getattr(s, 'rate_contract_base_url', '') or '').rstrip('/')
        self.token = getattr(s, 'rate_contract_token', None)

    async def search(self, origin: str, destination: str, equipment: str = '40HQ', etd: str | None = None):
        if not self.base:
            raise RuntimeError('RATE_CONTRACT_BASE_URL is required')
        headers = {'Authorization': f'Bearer {self.token}'} if self.token else {}
        params = {'origin': origin, 'destination': destination, 'equipment': equipment}
        if etd:
            params['etd'] = etd
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(self.base + '/v1/rates', headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
        return [_normalize_rate(x, origin, destination, equipment, source='contract')
                for x in (body if isinstance(body, list) else body.get('items', []))]


# --------------------------------------------------------------------------- #
# Index rates - skeleton
# --------------------------------------------------------------------------- #

class IndexRateProvider:
    """Skeleton for a paid spot-index feed (market reference pricing).

    Index rates are a *reference*, useful for sanity-checking a contract rate and for
    quoting spot cargo. They must be labelled as index-sourced in the quote trail so a
    customer-facing price can be traced back to a published index.
    """
    def __init__(self) -> None:
        s = get_settings()
        self.base = (getattr(s, 'rate_index_base_url', '') or '').rstrip('/')
        self.token = getattr(s, 'rate_index_token', None)

    async def search(self, origin: str, destination: str, equipment: str = '40HQ', etd: str | None = None):
        if not self.base:
            raise RuntimeError('RATE_INDEX_BASE_URL is required')
        headers = {'Authorization': f'Bearer {self.token}'} if self.token else {}
        params = {'from': origin, 'to': destination, 'containerType': equipment}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(self.base + '/rates', headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
        return [_normalize_rate(x, origin, destination, equipment, source='index')
                for x in (body if isinstance(body, list) else body.get('items', []))]


def _normalize_rate(x: dict, origin: str, destination: str, equipment: str, *, source: str) -> RateResult:
    """Flatten one vendor rate record into our canonical shape.

    Single place where vendor field-name drift is absorbed. `base_rate` falls back through
    the common vendor spellings rather than assuming one contract's vocabulary.
    """
    base = _first_number(x, 'baseRate', 'oceanFreight', 'base_rate', 'freight', 'amount')
    items: list[Surcharge] = []
    raw_items = x.get('surcharges') or x.get('chargeItems') or []
    if isinstance(raw_items, dict):
        raw_items = [{'code': k, 'amount': v} for k, v in raw_items.items()]
    for it in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(it, dict):
            continue
        amt = _first_number(it, 'amount', 'value', 'charge')
        if amt is None:
            continue
        items.append(Surcharge(
            code=str(it.get('code') or it.get('chargeCode') or 'MISC'),
            label=str(it.get('label') or it.get('name') or it.get('description') or 'Surcharge'),
            amount=amt,
            basis=str(it.get('basis') or 'per_container'),
        ))
    surcharges = _first_number(x, 'surchargesTotal', 'totalSurcharges')
    if surcharges is None:
        surcharges = round(sum(s.amount for s in items), 2)
    validity = x.get('validUntil') or x.get('validity') or x.get('expiryDate')
    return RateResult(
        source=source,
        carrier=x.get('carrier') or x.get('carrierCode') or x.get('scac'),
        currency=str(x.get('currency') or 'USD'),
        base_rate=float(base or 0.0),
        surcharges=float(surcharges),
        validity=validity,
        equipment=equipment, origin=origin, destination=destination,
        service=x.get('service') or x.get('serviceCode'),
        transit_days=_as_int(x.get('transitDays') or x.get('transitTime')),
        free_days=_as_int(x.get('freeDays') or x.get('demurrageFreeDays')),
        min_volume=_as_float(x.get('minVolume')),
        surcharge_breakdown=items, valid_until=validity,
        payload=x,
    )


def _first_number(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.replace(',', '').replace('$', '').strip())
            except ValueError:
                continue
    return None


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_rate_provider() -> RateProvider:
    name = get_settings().rate_provider
    if name == 'contract':
        return ContractRateProvider()
    if name == 'index':
        return IndexRateProvider()
    return MockRateProvider()
