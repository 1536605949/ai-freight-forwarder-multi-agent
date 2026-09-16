import pytest
from app.models import Inquiry,Customer
from app.services.rates import search_rates
from app.services.crm import add_activity
from app.services.growth import growth_opportunity

@pytest.mark.asyncio
async def test_rate_and_crm_growth(db):
    c=Customer(id='c',tenant_id='tenant-demo',company='Acme',email='a@acme.test')
    q=Inquiry(id='i',tenant_id='tenant-demo',customer_id='c',raw_message='x',origin='Shanghai',destination='Los Angeles',equipment='40HQ',quantity=1,etd='2026-09-20',missing_fields=[],status='ready')
    db.add_all([c,q]);db.commit()
    rates=await search_rates(db,'tenant-demo','u','i'); assert len(rates)==2 and all(x.total_cost>0 for x in rates)
    a=add_activity(db,'tenant-demo','u','c','email_reply','email','Customer asked about September sailing',{}); assert a.customer_id=='c'
    g=await growth_opportunity(db,'tenant-demo','t','c'); assert g['recommendation']
