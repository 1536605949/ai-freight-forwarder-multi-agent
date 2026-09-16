import pytest
from app.models import Inquiry
from app.services.schedule import search_schedules
@pytest.mark.asyncio
async def test_schedule(db):
 q=Inquiry(id='i',tenant_id='tenant-demo',raw_message='x',origin='Shanghai',destination='Los Angeles',equipment='40HQ',quantity=1,etd='2026-09-20',missing_fields=[],status='ready');db.add(q);db.commit(); rows=await search_schedules(db,'tenant-demo','u','i'); assert len(rows)>=3 and all(x.inquiry_id=='i' for x in rows)
