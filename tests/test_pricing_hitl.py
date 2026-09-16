import pytest
from app.models import Inquiry,RFQ,SupplierQuote,Customer
from app.services.pricing import optimize_quote
from app.services.approval import send_quote,review_quote
@pytest.mark.asyncio
async def test_pricing_and_hitl(db):
 c=Customer(id='c',tenant_id='tenant-demo',company='x',email='x@test'); i=Inquiry(id='i',tenant_id='tenant-demo',customer_id='c',raw_message='x',missing_fields=[],status='waiting_supplier_quotes'); r=RFQ(id='r',tenant_id='tenant-demo',inquiry_id='i',expected_suppliers=2,received_quotes=2,expires_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)); db.add_all([c,i,r,SupplierQuote(id='s1',tenant_id='tenant-demo',rfq_id='r',supplier_name='A',supplier_email='a@x',ocean_freight=1900,surcharges=100,reliability_score=.9),SupplierQuote(id='s2',tenant_id='tenant-demo',rfq_id='r',supplier_name='B',supplier_email='b@x',ocean_freight=2000,surcharges=50,reliability_score=.8)]);db.commit()
 q=await optimize_quote(db,'tenant-demo','u','trace','r'); assert q.sell_amount>=q.cost_amount+180
 with pytest.raises(ValueError): await send_quote(db,'tenant-demo','u',q.id)
 review_quote(db,'tenant-demo','reviewer',q.id,'approve','ok'); sent,_=await send_quote(db,'tenant-demo','u',q.id); assert sent.status=='sent'
