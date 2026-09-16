from sqlalchemy.orm import Session
from app.models import Booking,CustomerQuote,Inquiry
from app.enums import InquiryStatus
from app.providers.booking import get_booking_provider
from app.services.common import uid,audit
from app.state_machine import advance_status

async def create_booking(db:Session,tenant_id:str,actor:str,inquiry_id:str,quote_id:str,cargo_details:dict):
    q=db.query(CustomerQuote).filter_by(id=quote_id,tenant_id=tenant_id,inquiry_id=inquiry_id).first()
    if not q: raise KeyError('quote_not_found')
    if q.status!='sent': raise ValueError('customer_quote_must_be_sent_before_booking')
    existing=db.query(Booking).filter_by(tenant_id=tenant_id,inquiry_id=inquiry_id,quote_id=quote_id).first()
    if existing:return existing,True
    payload={'inquiry_id':inquiry_id,'quote_id':quote_id,'cargo':cargo_details}; resp=await get_booking_provider().create(payload)
    b=Booking(id=uid('bkg_'),tenant_id=tenant_id,inquiry_id=inquiry_id,quote_id=quote_id,provider_ref=resp.get('bookingReference'),status='confirmed' if str(resp.get('status','')).upper()=='CONFIRMED' else 'requested',payload=resp); db.add(b)
    inquiry=db.query(Inquiry).filter_by(id=inquiry_id,tenant_id=tenant_id).first(); advance_status(inquiry,InquiryStatus.BOOKED,context='booking.create')
    audit(db,tenant_id,actor,'booking.created','booking',b.id,{'provider_ref':b.provider_ref}); db.commit(); db.refresh(b); return b,False
