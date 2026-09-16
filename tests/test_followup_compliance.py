import pytest
from datetime import datetime,timezone,timedelta
from app.models import Customer,FollowUpTask
from app.services.followup import process_due
@pytest.mark.asyncio
async def test_do_not_contact(db):
 c=Customer(id='c',tenant_id='tenant-demo',company='x',email='x@x',preferences={'do_not_contact':True});t=FollowUpTask(id='f',tenant_id='tenant-demo',customer_id='c',due_at=datetime.now(timezone.utc)-timedelta(minutes=1),kind='sales',payload={});db.add_all([c,t]);db.commit(); ids=await process_due(db); assert 'f' in ids;db.refresh(t);assert t.status=='suppressed'
