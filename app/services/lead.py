from sqlalchemy.orm import Session
from app.models import Lead
from app.schemas import LeadCreate
from app.services.common import uid,audit

def score_lead(x:LeadCreate)->float:
    s=20.0
    if x.email: s+=15
    if x.trade_lane: s+=25
    if x.industry: s+=10
    if x.country: s+=10
    if x.source in {'referral','website','event'}: s+=10
    if x.consent_status=='opt_in': s+=10
    return min(s,100)

def create_lead(db:Session,tenant_id:str,actor:str,x:LeadCreate)->Lead:
    score=score_lead(x); status='qualified' if score>=65 else 'new'
    p=Lead(id=uid('lead_'),tenant_id=tenant_id,score=score,status=status,**x.model_dump(mode='json'))
    db.add(p); audit(db,tenant_id,actor,'lead.created','lead',p.id,{'score':score}); db.commit(); db.refresh(p); return p
