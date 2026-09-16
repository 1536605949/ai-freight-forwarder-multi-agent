from sqlalchemy.orm import Session
from app.models import Lead
from app.agents.specialists import run_agent

async def outreach_draft(db:Session,tenant_id:str,trace_id:str,lead_id:str):
    x=db.query(Lead).filter_by(id=lead_id,tenant_id=tenant_id).first()
    if not x: raise KeyError('lead_not_found')
    if x.status=='do_not_contact' or x.consent_status=='opt_out': raise ValueError('do_not_contact')
    r=await run_agent(db,tenant_id,trace_id,'lead',{'company':x.company,'contact_name':x.contact_name,'country':x.country,'industry':x.industry,'trade_lane':x.trade_lane,'score':x.score,'source':x.source,'consent_status':x.consent_status},False)
    return {'lead_id':x.id,'score':x.score,'draft':r.text,'send_allowed':x.consent_status in {'opt_in','legitimate_interest_reviewed'}}
