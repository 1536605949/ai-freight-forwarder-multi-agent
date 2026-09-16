from __future__ import annotations
import time, uuid
from datetime import datetime
from fastapi import FastAPI,Depends,HTTPException,Request,Header
from fastapi.responses import Response
from sqlalchemy.orm import Session
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from app.db import Base,engine,get_db
from app.auth import current_principal,Principal,require_role
from app.schemas import (
    ApprovalIn, BookingCreate, CRMActivityCreate, FollowUpCreate, InboundMessage,
    InquiryPatch, LeadCreate, OptOutIn, OrchestrateContinueIn, OrchestrateIn,
    OutreachApproveIn, OutreachDraftIn, PromoteIn, ProspectSearchIn, SupplierInboundEmail,
    SupplierQuoteIn, TrackingWebhook,
)
from app.services.lead import create_lead
from app.services.inquiry import ingest_message,patch_inquiry
from app.services.schedule import search_schedules
from app.services.rfq import create_rfq,add_supplier_quote
from app.services.pricing import optimize_quote
from app.services.approval import review_quote,send_quote
from app.services.booking import create_booking
from app.services.tracking import add_event
from app.services.followup import create_task,process_due
from app.services.rates import search_rates
from app.services.crm import add_activity
from app.services.growth import growth_opportunity
from app.services.lead_outreach import outreach_draft
from app.services.supplier_email import parse_and_store_supplier_email
from app.services import prospecting as prospecting_svc
from app.services import vessel as vessel_svc
from app.services import orchestrator as orchestrator_svc
from app.providers.tracking import get_tracking_provider
from app.models import Inquiry,SupplierQuote,CustomerQuote,Booking,TrackingEvent,Lead,Customer,AgentRun,AuditEvent,CRMActivity,RateSnapshot,ProspectCandidate,OutreachActivity
from app.metrics import REQUESTS
from app.logging_utils import configure_logging,log_event
from app.config import get_settings

configure_logging(); app=FastAPI(title='AI Freight Forwarder Multi-Agent Platform',version='1.0.0-rc1')

# Demo UI. Served only outside production so a bid/demo environment can open the pages
# directly; not part of the business API surface.
if get_settings().environment != 'production':
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path
    _demo=Path(__file__).resolve().parents[1]/'demo_client'
    if _demo.is_dir(): app.mount('/demo',StaticFiles(directory=str(_demo),html=True),name='demo')

@app.on_event('startup')
def startup():
    if get_settings().environment != 'production': Base.metadata.create_all(engine)

@app.middleware('http')
async def request_meta(req:Request,call_next):
    started=time.perf_counter(); request_id=req.headers.get('X-Request-ID') or str(uuid.uuid4()); trace_id=req.headers.get('X-Trace-ID') or request_id
    req.state.request_id=request_id; req.state.trace_id=trace_id
    try: resp=await call_next(req)
    except Exception: log_event('request.error',request_id=request_id,trace_id=trace_id,path=req.url.path); raise
    resp.headers['X-Request-ID']=request_id; resp.headers['X-Trace-ID']=trace_id; REQUESTS.labels(req.method,req.url.path,str(resp.status_code)).inc(); log_event('request.complete',request_id=request_id,trace_id=trace_id,path=req.url.path,status=resp.status_code,latency_ms=int((time.perf_counter()-started)*1000)); return resp

@app.get('/api/healthz')
def health(): return {'status':'ok','service':'freight-agent'}
@app.get('/metrics')
def metrics(): return Response(generate_latest(),media_type=CONTENT_TYPE_LATEST)

@app.post('/api/v1/leads')
def api_lead(x:LeadCreate,db:Session=Depends(get_db),p:Principal=Depends(current_principal)): return _row(create_lead(db,p.tenant_id,p.user_id,x))

@app.post('/api/v1/leads/{lead_id}/outreach-draft')
async def api_lead_outreach(lead_id:str,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return await outreach_draft(db,p.tenant_id,request.state.trace_id,lead_id)
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

# --- Prospecting: find and qualify new customers on a target lane ---------------------
@app.post('/api/v1/prospecting/search')
async def api_prospect_search(x:ProspectSearchIn,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    return await prospecting_svc.search_prospects(db,p.tenant_id,p.user_id,request.state.trace_id,x.lane,x.industry,x.limit,x.dry_run)

@app.get('/api/v1/prospects')
def api_prospects(status:str|None=None,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    q=db.query(ProspectCandidate).filter_by(tenant_id=p.tenant_id)
    if status: q=q.filter_by(status=status)
    return [_row(x) for x in q.order_by(ProspectCandidate.score.desc()).all()]

@app.get('/api/v1/prospects/{prospect_id}')
def api_prospect(prospect_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    x=db.query(ProspectCandidate).filter_by(id=prospect_id,tenant_id=p.tenant_id).first()
    if not x: raise HTTPException(404,'not found')
    return _row(x)

@app.post('/api/v1/prospects/{prospect_id}/enrich')
async def api_prospect_enrich(prospect_id:str,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return await prospecting_svc.enrich_prospect(db,p.tenant_id,p.user_id,request.state.trace_id,prospect_id)
    except KeyError:raise HTTPException(404,'not found')

@app.post('/api/v1/prospects/{prospect_id}/promote')
def api_prospect_promote(prospect_id:str,x:PromoteIn,db:Session=Depends(get_db),p:Principal=Depends(require_role('reviewer'))):
    try:return _row(prospecting_svc.promote_to_lead(db,p.tenant_id,p.user_id,prospect_id,x.consent_status,x.consent_basis))
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/prospects/{prospect_id}/outreach-draft')
async def api_prospect_outreach(prospect_id:str,x:OutreachDraftIn,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return _row(await prospecting_svc.create_outreach_draft(db,p.tenant_id,p.user_id,request.state.trace_id,prospect_id,x.subject))
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/outreach/{outreach_id}/review')
def api_outreach_review(outreach_id:str,x:OutreachApproveIn,db:Session=Depends(get_db),p:Principal=Depends(require_role('reviewer'))):
    try:return _row(prospecting_svc.approve_outreach(db,p.tenant_id,p.user_id,outreach_id,x.action,x.comment))
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/outreach/{outreach_id}/send')
async def api_outreach_send(outreach_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:o,r=await prospecting_svc.send_outreach(db,p.tenant_id,p.user_id,outreach_id); return {'outreach':_row(o),'message_id':getattr(r,'message_id',None)}
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/outreach/opt-out')
def api_opt_out(x:OptOutIn,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    return prospecting_svc.mark_opt_out(db,p.tenant_id,p.user_id,str(x.email),x.reason)

@app.post('/api/v1/crm/activities')
def api_crm_activity(x:CRMActivityCreate,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return _row(add_activity(db,p.tenant_id,p.user_id,x.customer_id,x.activity_type,x.channel,x.summary,x.payload))
    except KeyError:raise HTTPException(404,'not found')

@app.get('/api/v1/customers/{customer_id}/growth-opportunity')
async def api_growth(customer_id:str,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return await growth_opportunity(db,p.tenant_id,request.state.trace_id,customer_id)
    except KeyError:raise HTTPException(404,'not found')

@app.post('/api/v1/messages/inbound')
async def api_inbound(x:InboundMessage,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    return await ingest_message(db,p.tenant_id,p.user_id,request.state.trace_id,x)

@app.get('/api/v1/inquiries/{inquiry_id}')
def get_inquiry(inquiry_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    q=db.query(Inquiry).filter_by(id=inquiry_id,tenant_id=p.tenant_id).first();
    if not q: raise HTTPException(404,'not found')
    return _row(q)

@app.patch('/api/v1/inquiries/{inquiry_id}')
def update_inquiry(inquiry_id:str,x:InquiryPatch,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return _row(patch_inquiry(db,p.tenant_id,p.user_id,inquiry_id,x))
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/inquiries/{inquiry_id}/schedules')
async def schedules(inquiry_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return [_row(x) for x in await search_schedules(db,p.tenant_id,p.user_id,inquiry_id)]
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/inquiries/{inquiry_id}/rates')
async def rates(inquiry_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return [_row(x) for x in await search_rates(db,p.tenant_id,p.user_id,inquiry_id)]
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/inquiries/{inquiry_id}/rfq')
async def rfq(inquiry_id:str,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:r,results=await create_rfq(db,p.tenant_id,p.user_id,request.state.trace_id,inquiry_id); return {'rfq':_row(r),'sent':[x.__dict__ for x in results]}
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/supplier-email/inbound')
async def supplier_email_inbound(x:SupplierInboundEmail,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:
        q,idem=await parse_and_store_supplier_email(db,p.tenant_id,p.user_id,request.state.trace_id,x); return {'quote':_row(q),'idempotent':idem}
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/rfqs/{rfq_id}/supplier-quotes')
def supplier_quote(rfq_id:str,x:SupplierQuoteIn,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:q,idem=add_supplier_quote(db,p.tenant_id,p.user_id,rfq_id,x); return {'quote':_row(q),'idempotent':idem}
    except KeyError:raise HTTPException(404,'not found')

@app.post('/api/v1/rfqs/{rfq_id}/optimize')
async def optimize(rfq_id:str,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return _row(await optimize_quote(db,p.tenant_id,p.user_id,request.state.trace_id,rfq_id))
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/quotes/{quote_id}/review')
def review(quote_id:str,x:ApprovalIn,db:Session=Depends(get_db),p:Principal=Depends(require_role('reviewer'))):
    try:return _row(review_quote(db,p.tenant_id,p.user_id,quote_id,x.action,x.comment))
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/quotes/{quote_id}/send')
async def quote_send(quote_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:q,r=await send_quote(db,p.tenant_id,p.user_id,quote_id); return {'quote':_row(q),'message_id':r.message_id,'provider':r.provider}
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/bookings')
async def booking(x:BookingCreate,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:b,idem=await create_booking(db,p.tenant_id,p.user_id,x.inquiry_id,x.quote_id,x.cargo_details); return {'booking':_row(b),'idempotent':idem}
    except KeyError:raise HTTPException(404,'not found')
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/api/v1/bookings/{booking_id}/tracking/refresh')
async def tracking_refresh(booking_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    b=db.query(Booking).filter_by(id=booking_id,tenant_id=p.tenant_id).first()
    if not b: raise HTTPException(404,'not found')
    if not b.provider_ref: raise HTTPException(409,'booking_reference_missing')
    events=await get_tracking_provider().get_events(b.provider_ref)
    saved=[]
    for x in events:
        payload=TrackingWebhook(booking_id=b.id,event_code=x.get('eventCode','UNKNOWN'),event_time=x.get('eventTime',''),location=x.get('location'),description=x.get('description'),payload=x)
        e,_=add_event(db,p.tenant_id,p.user_id,payload); saved.append(_row(e))
    return saved

@app.post('/api/v1/tracking/webhook')
def tracking_webhook(x:TrackingWebhook,x_webhook_secret:str|None=Header(default=None),db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    s=get_settings()
    if s.auth_mode!='dev' and x_webhook_secret!=s.webhook_secret: raise HTTPException(401,'invalid webhook secret')
    try:e,idem=add_event(db,p.tenant_id,'tracking-webhook',x); return {'event':_row(e),'idempotent':idem}
    except KeyError:raise HTTPException(404,'not found')

# --- Vessel position (AIS): proactive delay detection --------------------------------
@app.post('/api/v1/vessels/{vessel_name}/refresh')
async def vessel_refresh(vessel_name:str,voyage:str|None=None,booking_id:str|None=None,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return await vessel_svc.refresh_position(db,p.tenant_id,p.user_id,vessel_name,voyage,booking_id)
    except KeyError:raise HTTPException(404,'not found')

@app.get('/api/v1/vessels/{vessel_name}/history')
def vessel_history(vessel_name:str,limit:int=20,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    return vessel_svc.position_history(db,p.tenant_id,vessel_name,limit)

@app.post('/api/v1/followups')
def followup(x:FollowUpCreate,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return _row(create_task(db,p.tenant_id,p.user_id,x.customer_id,x.inquiry_id,datetime.fromisoformat(x.due_at.replace('Z','+00:00')),x.kind,x.payload))
    except KeyError:raise HTTPException(404,'not found')

# --- Unified orchestration: run the whole workflow, stopping at every human gate ------
@app.post('/api/v1/orchestrate/run')
async def orchestrate_run(x:OrchestrateIn,request:Request,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    if x.start_stage and x.start_stage not in orchestrator_svc.STAGES:
        raise HTTPException(422,f'unknown stage; valid: {orchestrator_svc.STAGES}')
    try:
        run=await orchestrator_svc.run_pipeline(db,p.tenant_id,p.user_id,request.state.trace_id,x.inquiry_id,
            start_stage=x.start_stage or 'parse',auto_supplier_quotes=x.auto_supplier_quotes,
            vessel_name=x.vessel_name,auto_approve=x.auto_approve)
        return run.as_dict()
    except KeyError:raise HTTPException(404,'not found')

@app.post('/api/v1/orchestrate/continue')
async def orchestrate_continue(x:OrchestrateContinueIn,request:Request,db:Session=Depends(get_db),p:Principal=Depends(require_role('reviewer'))):
    """Resume a pipeline that stopped at the human gate. Requires a reviewer role."""
    try:
        run=await orchestrator_svc.resume_after_approval(db,p.tenant_id,p.user_id,request.state.trace_id,
            x.quote_id,vessel_name=x.vessel_name)
        return run.as_dict()
    except KeyError:raise HTTPException(404,'not found')

@app.get('/api/v1/orchestrate/status/{inquiry_id}')
def orchestrate_status(inquiry_id:str,db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    try:return orchestrator_svc.pipeline_status(db,p.tenant_id,inquiry_id)
    except KeyError:raise HTTPException(404,'not found')

@app.get('/api/v1/orchestrate/stages')
def orchestrate_stages(): return {'stages':orchestrator_svc.STAGES,'gated':sorted(orchestrator_svc.GATED_STAGES),'auto':sorted(orchestrator_svc.AUTO_STAGES)}

@app.post('/internal/run-due-followups')
async def due(db:Session=Depends(get_db),p:Principal=Depends(require_role('admin'))): return {'processed':await process_due(db)}

@app.get('/api/v1/dashboard')
def dashboard(db:Session=Depends(get_db),p:Principal=Depends(current_principal)):
    def count(model): return db.query(model).filter_by(tenant_id=p.tenant_id).count()
    return {'leads':count(Lead),'customers':count(Customer),'inquiries':count(Inquiry),'supplier_quotes':count(SupplierQuote),'customer_quotes':count(CustomerQuote),'bookings':count(Booking),'tracking_events':count(TrackingEvent),'agent_runs':count(AgentRun),'audit_events':count(AuditEvent),'crm_activities':count(CRMActivity),'rate_snapshots':count(RateSnapshot),'prospects':count(ProspectCandidate),'outreach':count(OutreachActivity)}

def _row(x):
    return {c.name:getattr(x,c.name) for c in x.__table__.columns}
