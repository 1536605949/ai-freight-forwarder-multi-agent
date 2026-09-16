from __future__ import annotations
import json
from sqlalchemy.orm import Session
from app.agents.runtime import get_runtime, AgentResult
from app.agents.prompts import PROMPTS

async def run_agent(db:Session, tenant_id:str, trace_id:str, name:str, payload:dict, expect_json:bool=False)->AgentResult:
    return await get_runtime().run(db,tenant_id,trace_id,name,PROMPTS[name],json.dumps(payload,ensure_ascii=False,default=str),expect_json)
