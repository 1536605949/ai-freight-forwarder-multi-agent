"""Vessel position tracking with proactive delay alerting.

The point of this module is to turn a passive lookup into an *active* signal: when a vessel
slips past the delay threshold, the customer should hear it from us before they notice
themselves. That is the difference between tracking and service.
"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import Booking, VesselPositionSnapshot, FollowUpTask, Customer, Inquiry
from app.providers.vessel import get_vessel_position_provider
from app.services.common import uid, audit
from app.config import get_settings
from app.metrics import VESSEL_DELAY_ALERTS


async def refresh_position(db:Session, tenant_id:str, actor:str, vessel_name:str, voyage:str|None=None, booking_id:str|None=None)->dict:
    """Fetch and persist a position snapshot. Returns the snapshot plus a delay assessment."""
    if booking_id and not db.query(Booking).filter_by(id=booking_id,tenant_id=tenant_id).first():
        raise KeyError('booking_not_found')
    pos=await get_vessel_position_provider().get_position(vessel_name,voyage)
    snap=VesselPositionSnapshot(id=uid('vps_'),tenant_id=tenant_id,booking_id=booking_id,
        vessel_name=pos.vessel_name,voyage=pos.voyage,mmsi=pos.mmsi,imo=pos.imo,
        latitude=pos.latitude,longitude=pos.longitude,speed_knots=pos.speed_knots,course_deg=pos.course_deg,
        nav_status=pos.nav_status,current_port=pos.current_port,next_port=pos.next_port,eta=pos.eta,
        delay_hours=pos.delay_hours,source=pos.source,payload=pos.payload or {})
    db.add(snap)
    s=get_settings(); threshold=s.vessel_delay_alert_hours
    delayed=bool(pos.delay_hours is not None and pos.delay_hours>=threshold)
    audit(db,tenant_id,actor,'vessel.position.refreshed','vessel',vessel_name,
        {'booking_id':booking_id,'delay_hours':pos.delay_hours,'delayed':delayed})
    db.commit(); db.refresh(snap)

    alert=None
    if delayed:
        alert=_raise_delay_alert(db,tenant_id,actor,booking_id,vessel_name,pos.delay_hours,pos.eta,threshold)
    return {'position':_row(snap),'delayed':delayed,'threshold_hours':threshold,'alert':alert}


def _raise_delay_alert(db:Session, tenant_id:str, actor:str, booking_id:str|None, vessel_name:str, delay:float, eta:str|None, threshold:float)->dict|None:
    """Create a follow-up task so a human/worker contacts the customer about the delay.

    Only meaningful when the snapshot is tied to a booking — without a booking there is no
    customer to notify, and we do not guess one.
    """
    if not booking_id: return None
    booking=db.query(Booking).filter_by(id=booking_id,tenant_id=tenant_id).first()
    if not booking: return None
    inquiry=db.query(Inquiry).filter_by(id=booking.inquiry_id,tenant_id=tenant_id).first()
    if not inquiry or not inquiry.customer_id: return None
    customer=db.query(Customer).filter_by(id=inquiry.customer_id,tenant_id=tenant_id).first()
    if not customer: return None
    if customer.preferences.get('do_not_contact'):
        VESSEL_DELAY_ALERTS.labels('suppressed').inc(); return {'suppressed':True,'reason':'do_not_contact'}

    # One open delay alert per booking: a vessel that stays late must not spawn a task per poll.
    existing=db.query(FollowUpTask).filter_by(tenant_id=tenant_id,customer_id=customer.id,
        kind='vessel_delay',status='pending').all()
    for t in existing:
        if (t.payload or {}).get('booking_id')==booking_id:
            VESSEL_DELAY_ALERTS.labels('deduplicated').inc()
            return {'deduplicated':True,'task_id':t.id}

    task=FollowUpTask(id=uid('fu_'),tenant_id=tenant_id,customer_id=customer.id,inquiry_id=inquiry.id,
        due_at=datetime.now(timezone.utc),kind='vessel_delay',status='pending',
        payload={'booking_id':booking_id,'vessel':vessel_name,'delay_hours':delay,'eta':eta,'threshold_hours':threshold})
    db.add(task)
    audit(db,tenant_id,actor,'vessel.delay.alerted','booking',booking_id,
        {'vessel':vessel_name,'delay_hours':delay,'task_id':task.id})
    db.commit()
    VESSEL_DELAY_ALERTS.labels('raised').inc()
    return {'task_id':task.id,'delay_hours':delay,'customer_id':customer.id}


def position_history(db:Session, tenant_id:str, vessel_name:str, limit:int=20)->list[dict]:
    rows=(db.query(VesselPositionSnapshot).filter_by(tenant_id=tenant_id,vessel_name=vessel_name)
          .order_by(VesselPositionSnapshot.created_at.desc()).limit(limit).all())
    return [_row(x) for x in rows]


def _row(x): return {c.name:getattr(x,c.name) for c in x.__table__.columns}
