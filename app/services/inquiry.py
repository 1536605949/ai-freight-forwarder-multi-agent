from __future__ import annotations
from sqlalchemy.orm import Session
from app.models import Customer, Inquiry
from app.enums import InquiryStatus
from app.schemas import InboundMessage, InquiryPatch
from app.services.common import uid,audit,outbox
from app.state_machine import CONTENT_EDITABLE, LOCKED, InquiryLocked, derived_inquiry_status
from app.agents.specialists import run_agent

REQUIRED=['origin','destination','equipment','quantity','etd']
def missing_fields(data:dict)->list[str]: return [k for k in REQUIRED if not data.get(k)]

def get_or_create_customer(db:Session,tenant_id:str,email:str,name:str|None)->Customer:
    c=db.query(Customer).filter_by(tenant_id=tenant_id,email=email).first()
    if c:return c
    c=Customer(id=uid('cus_'),tenant_id=tenant_id,company=email.split('@')[-1],contact_name=name,email=email); db.add(c); db.flush(); return c

async def ingest_message(db:Session,tenant_id:str,actor:str,trace_id:str,x:InboundMessage)->dict:
    existing=db.query(Inquiry).filter_by(tenant_id=tenant_id,external_message_id=x.external_message_id).first()
    if existing:return {'idempotent':True,'inquiry_id':existing.id,'status':existing.status,'missing_fields':existing.missing_fields}
    intent=(await run_agent(db,tenant_id,trace_id,'orchestrator',{'subject':x.subject,'body':x.body},True)).data or {'intent':'general'}
    if intent.get('intent')!='new_inquiry': return {'intent':intent.get('intent'),'handled':False,'message':'routed outside inquiry workflow'}
    parsed=(await run_agent(db,tenant_id,trace_id,'inquiry_parser',{'subject':x.subject,'body':x.body},True)).data or {}
    c=get_or_create_customer(db,tenant_id,str(x.sender_email),x.sender_name); miss=missing_fields(parsed)
    q=Inquiry(id=uid('inq_'),tenant_id=tenant_id,customer_id=c.id,external_message_id=x.external_message_id,raw_message=x.body,missing_fields=miss,status=InquiryStatus.NEEDS_CLARIFICATION if miss else InquiryStatus.READY,**{k:parsed.get(k) for k in ['origin','destination','equipment','quantity','etd','commodity','weight_kg','incoterm']})
    db.add(q); audit(db,tenant_id,actor,'inquiry.created','inquiry',q.id,{'missing_fields':miss});
    clarification=None
    if miss:
        outbox(db,tenant_id,'clarification.requested',q.id,{'missing_fields':miss})
        clarification=(await run_agent(db,tenant_id,trace_id,'clarification',{'missing_fields':miss,'known':parsed},False)).text
    db.commit(); return {'intent':'new_inquiry','inquiry_id':q.id,'status':q.status,'missing_fields':miss,'clarification_message':clarification}

def patch_inquiry(db:Session,tenant_id:str,actor:str,inquiry_id:str,x:InquiryPatch)->Inquiry:
    q=db.query(Inquiry).filter_by(id=inquiry_id,tenant_id=tenant_id).first()
    if not q: raise KeyError('inquiry_not_found')
    current=InquiryStatus(q.status)
    # A booking already exists, or the record is closed: the content is now part of what was
    # agreed and sent. Editing it would silently diverge our record from the customer's copy,
    # so refuse rather than corrupt history.
    if current in LOCKED: raise InquiryLocked(q.status)
    before={k:getattr(q,k) for k in REQUIRED}
    for k,v in x.model_dump(exclude_none=True).items(): setattr(q,k,v)
    q.missing_fields=missing_fields({k:getattr(q,k) for k in REQUIRED})
    # Status is re-derived ONLY while the inquiry is still being specified. Past that point the
    # status belongs to the pipeline, and a field edit must not rewind it. Before this guard,
    # PATCHing a `booked` inquiry reset it to `ready` -- a reproduced defect.
    if current in CONTENT_EDITABLE: q.status=derived_inquiry_status(q.missing_fields)
    audit(db,tenant_id,actor,'inquiry.updated','inquiry',q.id,
          {'missing_fields':q.missing_fields,'status':str(q.status),
           'changed_fields':[k for k in REQUIRED if before.get(k)!=getattr(q,k)]})
    db.commit(); db.refresh(q); return q
