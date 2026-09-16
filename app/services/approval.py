from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import CustomerQuote,Approval,Inquiry,Customer
from app.enums import InquiryStatus
from app.services.common import uid,audit
from app.state_machine import advance_status
from app.providers.email import get_email_provider
from app.metrics import QUOTES_APPROVED


def review_quote(db:Session,tenant_id:str,reviewer:str,quote_id:str,action:str,comment:str|None):
    q=db.query(CustomerQuote).filter_by(id=quote_id,tenant_id=tenant_id).first();
    if not q: raise KeyError('quote_not_found')
    if q.status not in {'waiting_approval','draft'}: raise ValueError('quote_not_reviewable')
    q.status='approved' if action=='approve' else 'rejected'; q.approved_by=reviewer; q.approved_at=datetime.now(timezone.utc) if action=='approve' else None
    db.add(Approval(id=uid('app_'),tenant_id=tenant_id,object_type='customer_quote',object_id=q.id,action=action,reviewer=reviewer,comment=comment))
    inquiry=db.query(Inquiry).filter_by(id=q.inquiry_id,tenant_id=tenant_id).first(); advance_status(inquiry,InquiryStatus.APPROVED if action=='approve' else InquiryStatus.REJECTED,context='quote.review')
    audit(db,tenant_id,reviewer,'quote.'+action,'customer_quote',q.id,{'comment':comment}); db.commit(); QUOTES_APPROVED.inc() if action=='approve' else None; return q

async def send_quote(db:Session,tenant_id:str,actor:str,quote_id:str):
    q=db.query(CustomerQuote).filter_by(id=quote_id,tenant_id=tenant_id).first();
    if not q: raise KeyError('quote_not_found')
    if q.status!='approved': raise ValueError('approval_required')
    inquiry=db.query(Inquiry).filter_by(id=q.inquiry_id,tenant_id=tenant_id).first(); customer=db.query(Customer).filter_by(id=inquiry.customer_id,tenant_id=tenant_id).first()
    result=await get_email_provider().send(customer.email,f'Freight quotation {q.id}',q.customer_message or f'Quotation: {q.currency} {q.sell_amount}')
    q.status='sent'; q.sent_at=datetime.now(timezone.utc); advance_status(inquiry,InquiryStatus.SENT,context='quote.send'); audit(db,tenant_id,actor,'quote.sent','customer_quote',q.id,{'message_id':result.message_id}); db.commit(); return q,result
