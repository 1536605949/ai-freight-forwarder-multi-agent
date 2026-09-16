from sqlalchemy.orm import Session
from app.agents.specialists import run_agent
from app.schemas import SupplierQuoteIn,SupplierInboundEmail
from app.services.rfq import add_supplier_quote

async def parse_and_store_supplier_email(db:Session,tenant_id:str,actor:str,trace_id:str,x:SupplierInboundEmail):
    r=await run_agent(db,tenant_id,trace_id,'supplier_quote_parser',{'body':x.body},True)
    d=r.data or {}
    quote=SupplierQuoteIn(supplier_name=x.supplier_name,supplier_email=x.supplier_email,carrier=x.carrier,currency=d.get('currency') or 'USD',ocean_freight=float(d.get('ocean_freight') or 0),surcharges=float(d.get('surcharges') or 0),transit_days=d.get('transit_days'),free_time_days=d.get('free_time_days'),validity=d.get('validity'),raw_payload={'body':x.body})
    if quote.ocean_freight<=0: raise ValueError('supplier_quote_price_missing')
    return add_supplier_quote(db,tenant_id,actor,x.rfq_id,quote)
