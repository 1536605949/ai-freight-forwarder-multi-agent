import pytest
from app.services.inquiry import ingest_message
from app.schemas import InboundMessage
@pytest.mark.asyncio
async def test_idempotency(db):
 x=InboundMessage(external_message_id='m1',sender_email='a@test.com',subject='quote',body='Need 1x40HQ from Shanghai to Los Angeles')
 a=await ingest_message(db,'tenant-demo','u','t1',x); b=await ingest_message(db,'tenant-demo','u','t2',x); assert a['inquiry_id']==b['inquiry_id'] and b['idempotent'] is True
