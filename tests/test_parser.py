import pytest
from app.agents.runtime import MockAgentRuntime
@pytest.mark.asyncio
async def test_parser(db):
 r=await MockAgentRuntime().run(db,'tenant-demo','t','inquiry_parser','', '{"body":"Need 2x40HQ from Shanghai to Los Angeles"}',True)
 assert r.data['quantity']==2 and r.data['equipment']=='40HQ' and r.data['origin']=='shanghai' and r.data['destination']=='los angeles'
