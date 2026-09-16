from sqlalchemy.orm import Session
from app.models import Booking,TrackingEvent
from app.services.common import uid,audit

def add_event(db:Session,tenant_id:str,actor:str,x):
    b=db.query(Booking).filter_by(id=x.booking_id,tenant_id=tenant_id).first();
    if not b: raise KeyError('booking_not_found')
    # Natural idempotency key for demo: booking+event+time+location
    existing=db.query(TrackingEvent).filter_by(tenant_id=tenant_id,booking_id=b.id,event_code=x.event_code,event_time=x.event_time,location=x.location).first()
    if existing:return existing,True
    e=TrackingEvent(id=uid('trk_'),tenant_id=tenant_id,booking_id=b.id,event_code=x.event_code,event_time=x.event_time,location=x.location,description=x.description,payload=x.payload); db.add(e); audit(db,tenant_id,actor,'tracking.event','booking',b.id,{'event_code':x.event_code}); db.commit(); db.refresh(e); return e,False
