import pytest
from datetime import datetime,timezone
from app.models import Inquiry,RFQ
from app.schemas import SupplierInboundEmail
from app.services.supplier_email import parse_and_store_supplier_email

@pytest.mark.asyncio
async def test_supplier_email_to_quote(db):
    db.add(Inquiry(id='i',tenant_id='tenant-demo',raw_message='x',missing_fields=[],status='waiting_supplier_quotes'))
    db.add(RFQ(id='r',tenant_id='tenant-demo',inquiry_id='i',expected_suppliers=2,expires_at=datetime.now(timezone.utc)));db.commit()
    x=SupplierInboundEmail(rfq_id='r',supplier_name='Carrier A',supplier_email='rate@carrier.example',body='Ocean freight $1950 + surcharge $120. Transit: 14 days. Free time: 7 days.')
    q,idem=await parse_and_store_supplier_email(db,'tenant-demo','u','t',x); assert not idem and q.ocean_freight==1950 and q.surcharges==120 and q.transit_days==14
