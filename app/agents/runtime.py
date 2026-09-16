from __future__ import annotations
import json, re, time, uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from sqlalchemy.orm import Session
from app.config import get_settings
from app.models import AgentRun
from app.metrics import AGENT_RUNS, AGENT_LATENCY

@dataclass
class AgentResult:
    text:str
    data:dict[str,Any]|None=None

class AgentRuntime:
    async def run(self, db:Session, tenant_id:str, trace_id:str, name:str, instructions:str, prompt:str, expect_json:bool=False) -> AgentResult: raise NotImplementedError

class MockAgentRuntime(AgentRuntime):
    async def run(self, db, tenant_id, trace_id, name, instructions, prompt, expect_json=False):
        started=time.perf_counter(); data=None
        if name=='inquiry_parser': data=_mock_parse(prompt); text=json.dumps(data,ensure_ascii=False)
        elif name=='supplier_quote_parser': data=_mock_supplier_quote(prompt); text=json.dumps(data,ensure_ascii=False)
        elif name=='orchestrator':
            low=prompt.lower(); intent='new_inquiry' if any(x in low for x in ['quote','rate','询价','40hq','20gp','shipping','运价']) else ('booking' if 'booking' in low or '订舱' in low else ('tracking' if 'track' in low or '跟踪' in low else 'general'))
            data={'intent':intent}; text=json.dumps(data)
        elif name=='prospecting': data=_mock_prospecting(prompt); text=json.dumps(data,ensure_ascii=False)
        elif name=='enrichment': data=_mock_enrichment(prompt); text=json.dumps(data,ensure_ascii=False)
        elif name=='cold_outreach': data=None; text=_mock_cold_outreach(prompt)
        else: text=f'[{name}] 已根据业务上下文生成草稿。'
        elapsed=time.perf_counter()-started
        db.add(AgentRun(id=str(uuid.uuid4()),tenant_id=tenant_id,agent_name=name,trace_id=trace_id,input_summary=prompt[:500],output_summary=text[:1000],status='success',latency_ms=int(elapsed*1000))); db.commit()
        AGENT_RUNS.labels(name,'success').inc(); AGENT_LATENCY.labels(name).observe(elapsed)
        return AgentResult(text=text,data=data)

class MicrosoftAgentFrameworkRuntime(AgentRuntime):
    async def run(self, db, tenant_id, trace_id, name, instructions, prompt, expect_json=False):
        started=time.perf_counter()
        try:
            from agent_framework import Agent
            from agent_framework.openai import OpenAIChatClient
            s=get_settings()
            client=OpenAIChatClient(model=s.openai_chat_model) if s.openai_chat_model else OpenAIChatClient()
            agent=Agent(client=client,name=name,instructions=instructions)
            result=await agent.run(prompt)
            text=str(result)
            data=None
            if expect_json:
                raw=_extract_json(text); data=json.loads(raw); text=raw
            status='success'
            return AgentResult(text=text,data=data)
        except Exception as e:
            status='error'; text=str(e)
            raise
        finally:
            elapsed=time.perf_counter()-started
            db.add(AgentRun(id=str(uuid.uuid4()),tenant_id=tenant_id,agent_name=name,trace_id=trace_id,input_summary=prompt[:500],output_summary=text[:1000] if 'text' in locals() else None,status=status,latency_ms=int(elapsed*1000))); db.commit()
            AGENT_RUNS.labels(name,status).inc(); AGENT_LATENCY.labels(name).observe(elapsed)

def get_runtime() -> AgentRuntime:
    return MicrosoftAgentFrameworkRuntime() if get_settings().agent_provider=='maf_openai' else MockAgentRuntime()

def _extract_json(text:str)->str:
    m=re.search(r'\{.*\}',text,re.S); return m.group(0) if m else text

def _mock_parse(text:str)->dict[str,Any]:
    low=text.lower(); out={k:None for k in ['origin','destination','equipment','quantity','etd','commodity','weight_kg','incoterm']}
    # Ports/cities intentionally small deterministic demo dictionary; production LLM/parser handles arbitrary values.
    cities=['shanghai','ningbo','shenzhen','qingdao','los angeles','long beach','rotterdam','hamburg','上海','宁波','深圳','青岛','洛杉矶','长滩','鹿特丹','汉堡']
    found=[c for c in cities if c in low]
    if found:
        out['origin']=found[0]
        if len(found)>1: out['destination']=found[1]
    m=re.search(r'(\d+)\s*[x×*]?\s*(40hq|40hc|40gp|20gp|20dv)',low)
    if m: out['quantity']=int(m.group(1)); out['equipment']=m.group(2).upper()
    else:
        m=re.search(r'(40hq|40hc|40gp|20gp|20dv)',low)
        if m: out['equipment']=m.group(1).upper(); out['quantity']=1
    m=re.search(r'(?:weight|重量)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(kg|kgs|ton|tons|吨)?',low)
    if m:
        val=float(m.group(1)); out['weight_kg']=val*1000 if (m.group(2) in ['ton','tons','吨']) else val
    for inc in ['fob','cif','exw','dap','ddp']:
        if inc in low: out['incoterm']=inc.upper()
    # ETD is a REQUIRED field, so failing to extract it makes an inquiry un-runnable: every
    # ingested message would land in `needs_clarification` forever. An explicit ETD marker
    # wins; a bare ISO date is accepted as a fallback. Demo-grade, but it keeps the ingestion
    # path able to reach the pipeline, which is what the demo and the eval harness need.
    etd=None
    m=re.search(r'etd\s*[:：]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})',low)
    if m: etd=m.group(1)
    else:
        m=re.search(r'(\d{4}-\d{2}-\d{2})',low)
        if m: etd=m.group(1)
    if etd: out['etd']=_normalise_date(etd)
    return out


def _normalise_date(raw:str)->str:
    """Return YYYY-MM-DD, or the input unchanged if it is not a recognisable date."""
    for fmt in ('%Y-%m-%d','%d/%m/%Y','%m/%d/%Y','%d/%m/%y','%m/%d/%y'):
        try: return datetime.strptime(raw,fmt).strftime('%Y-%m-%d')
        except ValueError: continue
    return raw


def _mock_supplier_quote(text:str)->dict[str,Any]:
    low=text.lower()
    nums=[float(x) for x in re.findall(r'\$\s*(\d+(?:\.\d+)?)',low)]
    ocean=nums[0] if nums else 0
    surcharge=nums[1] if len(nums)>1 else 0
    tr=re.search(r'(?:transit|tt)\s*[:：]?\s*(\d+)\s*(?:days|day|d)',low)
    ft=re.search(r'(?:free\s*time|free days)\s*[:：]?\s*(\d+)',low)
    return {'ocean_freight':ocean,'surcharges':surcharge,'transit_days':int(tr.group(1)) if tr else None,'free_time_days':int(ft.group(1)) if ft else None,'validity':None,'currency':'USD'}


def _mock_prospecting(text:str)->dict[str,Any]:
    """Deterministic qualification over the supplied candidate list.

    Scoring is intentionally transparent and rule-based: in mock mode we must not imply
    the LLM made a judgement it did not make. Same input -> same ranking.
    """
    try: payload=json.loads(text)
    except Exception: return {'qualified':[],'excluded':[]}
    cands=payload.get('candidates') or []
    lane=(payload.get('lane') or '').lower()
    qualified=[]; excluded=[]
    for c in cands:
        company=c.get('company') or 'unknown'
        ind=(c.get('industry') or '').lower()
        vol=c.get('est_volume_teu') or 0
        # Transparent heuristic: manufactured goods move better on container lanes than bulk-only trades.
        fit=40.0
        if any(k in ind for k in ['appliance','auto','furniture','textile','electronic','machinery','toy','building']): fit+=25
        if vol>=300: fit+=20
        elif vol>=100: fit+=12
        elif vol>0: fit+=5
        if c.get('country'): fit+=5
        fit=min(round(fit,1),100)
        if fit>=55:
            qualified.append({'company':company,'fit_score':fit,
                'reason':f'行业 {c.get("industry") or "未知"} 与集装箱航线匹配；已知货量约 {vol} TEU/年。',
                'suggested_angle':f'以 {(lane or "该航线")} 的舱位保障与目的港清关协同为切入点。'})
        else:
            excluded.append({'company':company,'reason':'行业适箱性或已知货量不足，优先度低。'})
    qualified.sort(key=lambda x:-x['fit_score'])
    return {'qualified':qualified,'excluded':excluded}


def _mock_enrichment(text:str)->dict[str,Any]:
    try: payload=json.loads(text)
    except Exception: payload={}
    company=payload.get('company') or '该企业'
    ind=payload.get('industry') or '制造/贸易'
    has_email=bool(payload.get('email'))
    readiness='high' if (has_email and payload.get('website')) else ('medium' if has_email else 'low')
    missing=[]
    if not has_email: missing.append('email')
    if not payload.get('contact_name'): missing.append('contact_name')
    return {'decision_maker_role':'物流/供应链负责人（待核实）',
            'company_profile':f'{company}，{ind}行业，{payload.get("country") or "未知"}。公开渠道可获得企业基本信息。',
            'likely_needs':'对稳定舱位、目的港时效与综合物流成本敏感。',
            'outreach_readiness':readiness,'missing_info':missing}


def _mock_cold_outreach(text:str)->str:
    try: payload=json.loads(text)
    except Exception: payload={}
    company=payload.get('company') or '贵司'
    lane=payload.get('lane') or '贵司常用航线'
    return (
        f'尊敬的 {company} 物流负责人：\n\n'
        f'我们是一家专注国际海运整柜与拼箱的国际货运代理企业，长期经营 {lane} 方向。\n'
        f'通过公开的企业信息了解到贵司有相关出口业务，希望有机会就舱位保障、目的港时效\n'
        f'与综合物流成本做一次简短的方案交流。\n\n'
        f'如需进一步沟通，可回复本邮件告知贵司常用航线与出运节奏，我们会安排对应航线的\n'
        f'操作同事与您对接。\n\n'
        f'如不希望再收到此类邮件，请回复"退订"，我们将立即停止联系。\n\n'
        f'顺祝商祺\n'
    )
