from sqlalchemy.orm import Session
from app.models import Customer,Inquiry,CRMActivity
from app.agents.specialists import run_agent

async def growth_opportunity(db:Session,tenant_id:str,trace_id:str,customer_id:str):
    c=db.query(Customer).filter_by(id=customer_id,tenant_id=tenant_id).first()
    if not c: raise KeyError('customer_not_found')
    inquiries=db.query(Inquiry).filter_by(customer_id=customer_id,tenant_id=tenant_id).order_by(Inquiry.created_at.desc()).limit(10).all()
    acts=db.query(CRMActivity).filter_by(customer_id=customer_id,tenant_id=tenant_id).order_by(CRMActivity.created_at.desc()).limit(10).all()
    result=await run_agent(db,tenant_id,trace_id,'growth',{'customer':{'company':c.company,'email':c.email,'preferences':c.preferences},'recent_inquiries':[{'origin':x.origin,'destination':x.destination,'status':x.status,'etd':x.etd} for x in inquiries],'recent_activities':[{'type':x.activity_type,'summary':x.summary} for x in acts]},False)
    return {'customer_id':customer_id,'recommendation':result.text}
