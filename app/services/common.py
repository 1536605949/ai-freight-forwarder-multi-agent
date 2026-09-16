import uuid
from sqlalchemy.orm import Session
from app.models import AuditEvent, OutboxEvent

def uid(prefix=''): return prefix+uuid.uuid4().hex

def audit(db:Session, tenant_id:str, actor:str,event_type:str,object_type:str,object_id:str,payload:dict|None=None):
    db.add(AuditEvent(id=uid('aud_'),tenant_id=tenant_id,actor=actor,event_type=event_type,object_type=object_type,object_id=object_id,payload=payload or {}))

def outbox(db:Session,tenant_id:str,event_type:str,aggregate_id:str,payload:dict|None=None):
    db.add(OutboxEvent(id=uid('evt_'),tenant_id=tenant_id,event_type=event_type,aggregate_id=aggregate_id,payload=payload or {}))
