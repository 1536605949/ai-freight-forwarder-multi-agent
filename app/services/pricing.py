from __future__ import annotations
from datetime import date,timedelta
from sqlalchemy.orm import Session
from app.models import RFQ,SupplierQuote,CustomerQuote,Inquiry
from app.enums import InquiryStatus
from app.config import get_settings
from app.services.common import uid,audit
from app.state_machine import advance_status
from app.agents.specialists import run_agent

def supplier_score(q:SupplierQuote)->float:
    total=q.ocean_freight+q.surcharges
    # Lower price/transit is better; more free time/reliability is better. Stable deterministic ranking.
    return total + (q.transit_days or 20)*12 - (q.free_time_days or 0)*8 - q.reliability_score*150

async def optimize_quote(db:Session,tenant_id:str,actor:str,trace_id:str,rfq_id:str):
    s=get_settings(); rfq=db.query(RFQ).filter_by(id=rfq_id,tenant_id=tenant_id).first()
    if not rfq: raise KeyError('rfq_not_found')
    quotes=db.query(SupplierQuote).filter_by(tenant_id=tenant_id,rfq_id=rfq_id).all()
    if len(quotes)<s.min_supplier_quotes: raise ValueError('insufficient_supplier_quotes')
    best=min(quotes,key=supplier_score); cost=round(best.ocean_freight+best.surcharges,2); margin=round(max(s.minimum_margin_usd,cost*s.default_margin_pct),2); sell=round(cost+margin,2)
    rationale=f'选择 {best.supplier_name}; cost={cost:.2f}; transit={best.transit_days}; free_time={best.free_time_days}; reliability={best.reliability_score:.2f}. 价格与利润由确定性 Pricing Engine 计算。'
    draft=(await run_agent(db,tenant_id,trace_id,'quotation',{'supplier':best.supplier_name,'cost':cost,'margin':margin,'sell':sell,'valid_days':s.quote_valid_days,'rationale':rationale},False)).text
    cq=CustomerQuote(id=uid('cq_'),tenant_id=tenant_id,inquiry_id=rfq.inquiry_id,supplier_quote_id=best.id,cost_amount=cost,margin_amount=margin,sell_amount=sell,status='waiting_approval',rationale=rationale,customer_message=draft,valid_until=(date.today()+timedelta(days=s.quote_valid_days)).isoformat()); db.add(cq)
    inquiry=db.query(Inquiry).filter_by(id=rfq.inquiry_id,tenant_id=tenant_id).first(); advance_status(inquiry,InquiryStatus.WAITING_APPROVAL,context='pricing.optimize')
    audit(db,tenant_id,actor,'quote.optimized','customer_quote',cq.id,{'supplier_quote_id':best.id,'sell':sell}); db.commit(); db.refresh(cq); return cq
