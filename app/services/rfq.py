from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.models import Inquiry, RFQ, SupplierQuote
from app.enums import InquiryStatus
from app.providers.email import get_email_provider
from app.services.common import uid,audit
from app.state_machine import advance_status
from app.agents.specialists import run_agent
from app.config import get_settings
from app.metrics import RFQ_SENT

DEMO_SUPPLIERS=[('Ocean Partner A','a@carrier.demo'),('Ocean Partner B','b@carrier.demo'),('Ocean Partner C','c@carrier.demo'),('Ocean Partner D','d@carrier.demo'),('Ocean Partner E','e@carrier.demo')]

async def create_rfq(db:Session,tenant_id:str,actor:str,trace_id:str,inquiry_id:str,suppliers:list[tuple[str,str]]|None=None):
    q=db.query(Inquiry).filter_by(id=inquiry_id,tenant_id=tenant_id).first();
    if not q: raise KeyError('inquiry_not_found')
    if q.missing_fields: raise ValueError('inquiry_incomplete')
    if db.query(RFQ).filter_by(tenant_id=tenant_id,inquiry_id=q.id,status='open').first(): raise ValueError('open_rfq_exists')
    suppliers=suppliers or DEMO_SUPPLIERS; s=get_settings(); rfq=RFQ(id=uid('rfq_'),tenant_id=tenant_id,inquiry_id=q.id,expected_suppliers=len(suppliers),expires_at=datetime.now(timezone.utc)+timedelta(hours=s.rfq_timeout_hours)); db.add(rfq); db.flush()
    draft=(await run_agent(db,tenant_id,trace_id,'supplier_rfq',{'inquiry':{k:getattr(q,k) for k in ['origin','destination','equipment','quantity','etd','commodity','weight_kg']}},False)).text
    email=get_email_provider()
    async def one(name,addr): return await email.send(addr,f'RFQ {rfq.id}: {q.origin} → {q.destination}',draft+'\n\n'+f'Equipment: {q.quantity} x {q.equipment}; ETD: {q.etd}')
    results=await asyncio.gather(*(one(n,a) for n,a in suppliers))
    RFQ_SENT.inc(len(results)); advance_status(q,InquiryStatus.WAITING_SUPPLIER_QUOTES,context='rfq.create'); audit(db,tenant_id,actor,'rfq.sent','rfq',rfq.id,{'suppliers':[x[1] for x in suppliers]}); db.commit(); return rfq,results

def add_supplier_quote(db:Session,tenant_id:str,actor:str,rfq_id:str,x):
    rfq=db.query(RFQ).filter_by(id=rfq_id,tenant_id=tenant_id).first();
    if not rfq: raise KeyError('rfq_not_found')
    existing=db.query(SupplierQuote).filter_by(tenant_id=tenant_id,rfq_id=rfq_id,supplier_email=str(x.supplier_email)).first()
    if existing:return existing,True
    q=SupplierQuote(id=uid('sq_'),tenant_id=tenant_id,rfq_id=rfq_id,**x.model_dump(mode='json')); db.add(q); rfq.received_quotes+=1
    if rfq.received_quotes>=rfq.expected_suppliers: rfq.status='complete'
    audit(db,tenant_id,actor,'supplier_quote.received','rfq',rfq.id,{'supplier':str(x.supplier_email)}); db.commit(); db.refresh(q); return q,False
