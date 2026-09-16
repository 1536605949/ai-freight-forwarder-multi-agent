from sqlalchemy.orm import Session
from app.models import Customer,CRMActivity
from app.services.common import uid,audit

def add_activity(db:Session,tenant_id:str,actor:str,customer_id:str,activity_type:str,channel:str,summary:str,payload:dict):
    if not db.query(Customer).filter_by(id=customer_id,tenant_id=tenant_id).first(): raise KeyError('customer_not_found')
    a=CRMActivity(id=uid('crm_'),tenant_id=tenant_id,customer_id=customer_id,activity_type=activity_type,channel=channel,summary=summary,payload=payload)
    db.add(a); audit(db,tenant_id,actor,'crm.activity','customer',customer_id,{'activity_type':activity_type}); db.commit(); db.refresh(a); return a
