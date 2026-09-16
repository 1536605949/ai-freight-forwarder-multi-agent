from sqlalchemy.orm import Session
from app.models import Inquiry,RateSnapshot
from app.providers.rates import get_rate_provider, Surcharge
from app.services.common import uid,audit

async def search_rates(db:Session,tenant_id:str,actor:str,inquiry_id:str):
    q=db.query(Inquiry).filter_by(id=inquiry_id,tenant_id=tenant_id).first()
    if not q: raise KeyError('inquiry_not_found')
    if q.missing_fields: raise ValueError('inquiry_incomplete')
    rows=await get_rate_provider().search(q.origin,q.destination,q.equipment,q.etd)
    db.query(RateSnapshot).filter_by(tenant_id=tenant_id,inquiry_id=q.id).delete()
    out=[]
    for x in rows:
        # Canonical money fields go in their own columns; everything that enriches a quote
        # (breakdown, validity, transit, provenance) is folded into payload so it survives
        # to the pricing stage without a schema change per vendor.
        payload=dict(x.payload or {})
        payload.update({
            'equipment':x.equipment,'origin':x.origin,'destination':x.destination,
            'service':x.service,'transit_days':x.transit_days,'free_days':x.free_days,
            'min_volume':x.min_volume,'valid_until':x.valid_until or x.validity,
            'surcharge_breakdown':[s.__dict__ if isinstance(s,Surcharge) else s for s in (x.surcharge_breakdown or [])],
        })
        r=RateSnapshot(id=uid('rate_'),tenant_id=tenant_id,inquiry_id=q.id,source=x.source,carrier=x.carrier,currency=x.currency,base_rate=x.base_rate,surcharges=x.surcharges,total_cost=x.total_cost,validity=x.validity,payload=payload)
        db.add(r); out.append(r)
    audit(db,tenant_id,actor,'rates.searched','inquiry',q.id,{'count':len(out),'sources':sorted({x.source for x in out})}); db.commit(); return out
