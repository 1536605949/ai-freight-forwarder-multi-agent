"""Prospecting data sources.

A `ProspectingSource` answers one question: "which companies plausibly ship on this lane?"
It returns *unverified vendor data*. Nothing here writes to the CRM — candidates land in
`prospect_candidates` and must clear enrichment + compliance review before becoming a Lead.

Production note: in mainland China, customs bill-of-lading data is not open to ordinary
enterprises; it is available only through licensed data vendors. Real deployments must
contract such a vendor (or use public business registries / trade-show lists) and record
the lawful basis in `data_origin`. The Mock source exists so the workflow is demonstrable
without a commercial data contract.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
from app.config import get_settings

@dataclass
class ProspectRecord:
    company:str
    country:str|None=None
    industry:str|None=None
    website:str|None=None
    trade_lane:str|None=None
    est_volume_teu:float|None=None
    source:str='mock'
    source_ref:str|None=None
    data_origin:str='mock_synthetic'
    payload:dict=field(default_factory=dict)

class ProspectingSource:
    async def search(self, lane:str, industry:str|None=None, limit:int=20)->list[ProspectRecord]:
        raise NotImplementedError

class MockProspectingSource:
    """Deterministic synthetic prospects, so a demo is reproducible run to run.

    Determinism matters here: a bid demo that returns different companies every refresh
    looks broken. The same lane always yields the same set.
    """
    _INDUSTRIES=['Household Appliances','Auto Parts','Furniture','Textiles','Consumer Electronics','Machinery','Building Materials','Toys']
    _SUFFIX=['Trading','Industrial','Import & Export','Manufacturing','Global Sourcing','Supply Chain']
    _MARKETS=[('US','United States'),('DE','Germany'),('NL','Netherlands'),('GB','United Kingdom'),('AU','Australia'),('BR','Brazil'),('JP','Japan'),('AE','United Arab Emirates')]

    async def search(self, lane, industry=None, limit=20):
        origin,destination=_split_lane(lane)
        seed=int(hashlib.sha256(lane.encode('utf-8')).hexdigest()[:8],16)
        out=[]
        for i in range(limit):
            h=hashlib.sha256(f'{lane}:{i}:{seed}'.encode('utf-8')).hexdigest()
            ind=industry or self._INDUSTRIES[int(h[:2],16)%len(self._INDUSTRIES)]
            mk=self._MARKETS[int(h[2:4],16)%len(self._MARKETS)]
            company=f'{_root_name(h)} {self._SUFFIX[int(h[4:6],16)%len(self._SUFFIX)]}'
            slug=company.lower().replace(' ','').replace('&','')
            out.append(ProspectRecord(
                company=company, country=mk[1], industry=ind,
                website=f'https://www.{slug}.example',
                trade_lane=lane, est_volume_teu=round(20+ (int(h[6:10],16)%900) + (int(h[10:12],16)%100)/100,2),
                source='mock', source_ref=f'{destination[:3].upper()}-{h[:10].upper()}',
                data_origin='mock_synthetic',
                payload={'origin':origin,'destination':destination,'demo':True},
            ))
        return out

class LicensedVendorProspectingSource:
    """Skeleton for a contracted trade-data vendor (bill of lading / customs data).

    Intentionally not implemented: the endpoint, auth model and field mapping are
    contract-specific. Normalize the vendor payload into `ProspectRecord` here so the
    rest of the application never sees vendor-specific shapes.
    """
    def __init__(self):
        s=get_settings(); self.base=(getattr(s,'prospecting_base_url','') or '').rstrip('/'); self.token=getattr(s,'prospecting_token',None)
    async def search(self, lane, industry=None, limit=20):
        if not self.base: raise RuntimeError('PROSPECTING_BASE_URL is required')
        # Auth header shape is vendor-specific; build it once the contract is known, then
        # normalize the response into ProspectRecord below.
        raise NotImplementedError('Map your vendor payload into ProspectRecord here; contract-specific.')

def _split_lane(lane:str)->tuple[str,str]:
    for sep in ('->','→','-','to'):
        if sep in lane:
            a,b=lane.split(sep,1)
            return a.strip(),b.strip()
    return lane.strip(),''


def _root_name(h:str)->str:
    roots=['Meridian','Northfield','Cobalt','Harborline','Vantage','Ironwood','Bluestem','Keystone','Larkspur','Stonebridge','Aurora','Cardinal']
    return roots[int(h[12:16],16)%len(roots)]

def get_prospecting_source()->ProspectingSource:
    return LicensedVendorProspectingSource() if get_settings().prospecting_provider=='licensed_vendor' else MockProspectingSource()
