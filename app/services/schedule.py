from sqlalchemy.orm import Session
from app.models import Inquiry, ScheduleOption
from app.enums import InquiryStatus
from app.providers.schedule import get_schedule_provider
from app.services.common import uid,audit
from app.state_machine import advance_status

async def search_schedules(db:Session,tenant_id:str,actor:str,inquiry_id:str):
    q=db.query(Inquiry).filter_by(id=inquiry_id,tenant_id=tenant_id).first()
    if not q: raise KeyError('inquiry_not_found')
    if q.missing_fields: raise ValueError('inquiry_incomplete')
    rows=await get_schedule_provider().search(q.origin,q.destination,q.etd)
    db.query(ScheduleOption).filter_by(tenant_id=tenant_id,inquiry_id=q.id).delete()
    saved=[]
    for x in rows:
        r=ScheduleOption(id=uid('sch_'),tenant_id=tenant_id,inquiry_id=q.id,carrier=x.carrier,service=x.service,vessel=x.vessel,voyage=x.voyage,origin=x.origin,destination=x.destination,etd=x.etd,eta=x.eta,transit_days=x.transit_days,direct=x.direct,co2e_kg=x.co2e_kg,source=x.source,payload=x.payload or {})
        db.add(r); saved.append(r)
    advance_status(q,InquiryStatus.SCHEDULED,context='schedule.search'); audit(db,tenant_id,actor,'schedule.searched','inquiry',q.id,{'count':len(saved)}); db.commit(); return saved
