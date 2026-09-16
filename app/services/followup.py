from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import FollowUpTask,Customer
from app.services.common import uid,audit
from app.agents.specialists import run_agent
from app.providers.email import get_email_provider


def create_task(db:Session,tenant_id:str,actor:str,customer_id:str,inquiry_id:str|None,due_at:datetime,kind:str,payload:dict):
    if not db.query(Customer).filter_by(id=customer_id,tenant_id=tenant_id).first(): raise KeyError('customer_not_found')
    t=FollowUpTask(id=uid('fu_'),tenant_id=tenant_id,customer_id=customer_id,inquiry_id=inquiry_id,due_at=due_at,kind=kind,payload=payload); db.add(t); audit(db,tenant_id,actor,'followup.created','followup',t.id,{}); db.commit(); return t

async def process_due(db:Session,limit:int=50):
    now=datetime.now(timezone.utc); tasks=db.query(FollowUpTask).filter(FollowUpTask.status=='pending',FollowUpTask.due_at<=now).limit(limit).all(); processed=[]
    for t in tasks:
        c=db.query(Customer).filter_by(id=t.customer_id,tenant_id=t.tenant_id).first()
        if not c: t.status='failed'; continue
        # Compliance hard gate: an explicit do_not_contact preference prevents automated outreach.
        if c.preferences.get('do_not_contact'): t.status='suppressed'; processed.append(t.id); continue
        trace='worker-'+t.id
        draft=(await run_agent(db,t.tenant_id,trace,'followup',{'customer':c.email,'kind':t.kind,'context':t.payload},False)).text
        await get_email_provider().send(c.email,'Following up on your freight requirements',draft); t.status='completed'; processed.append(t.id)
    db.commit(); return processed
