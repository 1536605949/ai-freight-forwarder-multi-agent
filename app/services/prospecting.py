"""Prospecting → enrichment → compliant cold outreach.

Division of labour, same as the rest of this codebase:
  - Agents reason and write: which prospects fit, what the company profile is, what the email says.
  - Deterministic code decides: who may be contacted, how often, and whether a message may leave.

The compliance gates below are deliberately NOT prompt instructions. A model must never be
the thing standing between a business and a privacy violation.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.models import ProspectCandidate, OutreachActivity, Lead
from app.providers.prospecting import get_prospecting_source
from app.providers.email import get_email_provider
from app.services.common import uid, audit
from app.agents.specialists import run_agent
from app.config import get_settings
from app.metrics import PROSPECTS_DISCOVERED, OUTREACH_BLOCKED

# Consent states that permit a first cold contact. Everything else is blocked.
OUTREACH_ALLOWED_BASIS={'opt_in','legitimate_interest_reviewed'}


async def search_prospects(db:Session,tenant_id:str,actor:str,trace_id:str,lane:str,industry:str|None=None,limit:int|None=None,dry_run:bool=False)->dict:
    """Discover candidates on a lane and have the Prospecting Agent rank them.

    `dry_run=True` returns the agent's ranking without persisting anything — useful for a
    demo where the operator wants to preview before committing prospects to the CRM pipeline.
    """
    s=get_settings(); limit=limit or s.prospect_default_limit
    records=await get_prospecting_source().search(lane,industry,limit)
    PROSPECTS_DISCOVERED.inc(len(records))

    payload={'lane':lane,'industry':industry,'candidates':[
        {'company':r.company,'country':r.country,'industry':r.industry,'website':r.website,
         'trade_lane':r.trade_lane,'est_volume_teu':r.est_volume_teu,'source':r.source} for r in records]}
    result=await run_agent(db,tenant_id,trace_id,'prospecting',payload,True)
    data=result.data or {'qualified':[],'excluded':[]}

    by_name={r.company:r for r in records}
    rationale={q.get('company'):q for q in data.get('qualified',[])}
    excluded={e.get('company'):e.get('reason') for e in data.get('excluded',[])}

    if dry_run:
        return {'lane':lane,'discovered':len(records),'qualified':len(rationale),'dry_run':True,
                'candidates':[{'company':r.company,'country':r.country,'industry':r.industry,
                    'est_volume_teu':r.est_volume_teu,'fit_score':rationale.get(r.company,{}).get('fit_score',0),
                    'reason':rationale.get(r.company,{}).get('reason') or excluded.get(r.company,'')} for r in records]}

    saved=[]
    for name,q in rationale.items():
        r=by_name.get(name)
        if not r: continue
        # Idempotency on (tenant, source, source_ref): re-running a search must not duplicate.
        existing=db.query(ProspectCandidate).filter_by(tenant_id=tenant_id,source=r.source,source_ref=r.source_ref).first()
        if existing:
            existing.score=q.get('fit_score') or existing.score; existing.score_rationale=q.get('reason'); saved.append(existing); continue
        p=ProspectCandidate(id=uid('prs_'),tenant_id=tenant_id,company=r.company,country=r.country,industry=r.industry,
            website=r.website,trade_lane=r.trade_lane,est_volume_teu=r.est_volume_teu,source=r.source,source_ref=r.source_ref,
            data_origin=r.data_origin,raw_payload=r.payload,status='discovered',score=q.get('fit_score') or 0,
            score_rationale=q.get('reason'))
        db.add(p); saved.append(p)
    audit(db,tenant_id,actor,'prospecting.searched','lane',lane,{'discovered':len(records),'qualified':len(saved)})
    db.commit()
    for p in saved: db.refresh(p)
    return {'lane':lane,'discovered':len(records),'qualified':len(saved),'prospects':[_row(p) for p in saved]}


async def enrich_prospect(db:Session,tenant_id:str,actor:str,trace_id:str,prospect_id:str)->dict:
    p=db.query(ProspectCandidate).filter_by(id=prospect_id,tenant_id=tenant_id).first()
    if not p: raise KeyError('prospect_not_found')
    r=await run_agent(db,tenant_id,trace_id,'enrichment',{'company':p.company,'country':p.country,'industry':p.industry,
        'website':p.website,'trade_lane':p.trade_lane,'est_volume_teu':p.est_volume_teu,'email':p.email},True)
    d=r.data or {}
    p.status='enriched'
    # The agent may only fill fields it can justify. Deterministic code owns consent state —
    # a model must never assert that a company consented to being contacted.
    if not p.consent_basis:
        p.consent_basis='public_business_information_pending_review'
    audit(db,tenant_id,actor,'prospect.enriched','prospect',p.id,{'readiness':d.get('outreach_readiness')})
    db.commit(); db.refresh(p)
    return {'prospect':_row(p),'enrichment':d}


def promote_to_lead(db:Session,tenant_id:str,actor:str,prospect_id:str,consent_status:str,consent_basis:str|None=None)->Lead:
    """Move a verified prospect into the CRM as a Lead.

    Consent status must be supplied by a human reviewer, not inferred. This is the single
    checkpoint where third-party data becomes a first-party customer record.
    """
    p=db.query(ProspectCandidate).filter_by(id=prospect_id,tenant_id=tenant_id).first()
    if not p: raise KeyError('prospect_not_found')
    if p.status=='promoted' and p.lead_id:
        existing=db.query(Lead).filter_by(id=p.lead_id,tenant_id=tenant_id).first()
        if existing: return existing
    if consent_status not in OUTREACH_ALLOWED_BASIS | {'unknown'}:
        raise ValueError('invalid_consent_status')
    lead=Lead(id=uid('lead_'),tenant_id=tenant_id,company=p.company,contact_name=p.contact_name,
        email=p.email,country=p.country,industry=p.industry,source=p.source,trade_lane=p.trade_lane,
        score=p.score,status='qualified' if p.score>=65 else 'new',consent_status=consent_status,
        origin_prospect_id=p.id,data_origin=p.data_origin,score_rationale=p.score_rationale,
        notes=consent_basis)
    db.add(lead); p.status='promoted'; p.lead_id=lead.id; p.consent_status=consent_status; p.consent_basis=consent_basis
    audit(db,tenant_id,actor,'prospect.promoted','lead',lead.id,{'prospect_id':p.id,'consent_status':consent_status,'basis':consent_basis})
    db.commit(); db.refresh(lead); return lead


async def create_outreach_draft(db:Session,tenant_id:str,actor:str,trace_id:str,prospect_id:str,subject:str|None=None)->OutreachActivity:
    """Generate a draft. Drafting is always allowed; sending is what gets gated."""
    p=db.query(ProspectCandidate).filter_by(id=prospect_id,tenant_id=tenant_id).first()
    if not p: raise KeyError('prospect_not_found')
    if not p.email: raise ValueError('prospect_email_missing')
    body=(await run_agent(db,tenant_id,trace_id,'cold_outreach',{'company':p.company,'country':p.country,
        'industry':p.industry,'lane':p.trade_lane,'fit_reason':p.score_rationale},False)).text
    o=OutreachActivity(id=uid('out_'),tenant_id=tenant_id,prospect_id=p.id,channel='email',recipient=p.email,
        subject=subject or f'{p.trade_lane or "国际海运"} 航线合作洽谈',body=body,status='draft',
        compliance_basis=p.consent_basis,trace_id=trace_id)
    db.add(o); audit(db,tenant_id,actor,'outreach.drafted','outreach',o.id,{'prospect_id':p.id})
    db.commit(); db.refresh(o); return o


def _frequency_block(db:Session,tenant_id:str,recipient:str)->int:
    s=get_settings()
    since=datetime.now(timezone.utc)-timedelta(days=s.outreach_window_days)
    return db.query(OutreachActivity).filter(OutreachActivity.tenant_id==tenant_id,
        OutreachActivity.recipient==recipient,OutreachActivity.status=='sent',
        OutreachActivity.sent_at>=since).count()


def approve_outreach(db:Session,tenant_id:str,reviewer:str,outreach_id:str,action:str,comment:str|None=None)->OutreachActivity:
    o=db.query(OutreachActivity).filter_by(id=outreach_id,tenant_id=tenant_id).first()
    if not o: raise KeyError('outreach_not_found')
    if o.status!='draft': raise ValueError('outreach_not_reviewable')
    o.status='approved' if action=='approve' else 'rejected'
    o.approved_by=reviewer; o.approved_at=datetime.now(timezone.utc) if action=='approve' else None
    audit(db,tenant_id,reviewer,'outreach.'+action,'outreach',o.id,{'comment':comment})
    db.commit(); db.refresh(o); return o


async def send_outreach(db:Session,tenant_id:str,actor:str,outreach_id:str)->tuple[OutreachActivity,object]:
    """Send an approved cold outreach message.

    Three independent gates, all in code:
      1. human approval recorded,
      2. a lawful basis on file,
      3. frequency cap for this recipient not exceeded.
    Any one failing blocks the send — no prompt can bypass this.
    """
    s=get_settings(); o=db.query(OutreachActivity).filter_by(id=outreach_id,tenant_id=tenant_id).first()
    if not o: raise KeyError('outreach_not_found')
    if s.outreach_requires_human_approval and o.status!='approved':
        OUTREACH_BLOCKED.labels('not_approved').inc(); raise ValueError('approval_required')

    p=db.query(ProspectCandidate).filter_by(id=o.prospect_id,tenant_id=tenant_id).first() if o.prospect_id else None
    basis=(o.compliance_basis or (p.consent_basis if p else None) or '')
    if basis not in OUTREACH_ALLOWED_BASIS:
        OUTREACH_BLOCKED.labels('no_lawful_basis').inc(); raise ValueError('compliance_basis_required')

    if _frequency_block(db,tenant_id,o.recipient)>=s.outreach_max_per_window:
        OUTREACH_BLOCKED.labels('frequency_cap').inc(); raise ValueError('frequency_cap_exceeded')

    result=await get_email_provider().send(o.recipient,o.subject or '合作洽谈',o.body)
    o.status='sent'; o.sent_at=datetime.now(timezone.utc); o.provider_message_id=getattr(result,'message_id',None)
    if p: p.status='contacted'
    audit(db,tenant_id,actor,'outreach.sent','outreach',o.id,{'recipient':o.recipient,'basis':basis})
    db.commit(); db.refresh(o); return o,result


def mark_opt_out(db:Session,tenant_id:str,actor:str,email:str,reason:str|None=None)->dict:
    """Honour an opt-out immediately across prospects, leads and outreach records."""
    affected={'prospects':0,'leads':0,'outreach':0}
    for p in db.query(ProspectCandidate).filter_by(tenant_id=tenant_id,email=email).all():
        p.consent_status='opt_out'; p.status='rejected'; affected['prospects']+=1
    for lead in db.query(Lead).filter_by(tenant_id=tenant_id,email=email).all():
        lead.consent_status='opt_out'; lead.status='do_not_contact'; affected['leads']+=1
    for o in db.query(OutreachActivity).filter_by(tenant_id=tenant_id,recipient=email).filter(OutreachActivity.status.in_(['draft','approved'])).all():
        o.status='suppressed'; affected['outreach']+=1
    audit(db,tenant_id,actor,'outreach.opt_out','email',email,{'reason':reason,**affected})
    db.commit(); return affected


def _row(x):
    return {c.name:getattr(x,c.name) for c in x.__table__.columns}
