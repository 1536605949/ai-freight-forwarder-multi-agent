from app.models import Inquiry
def test_tenant_isolation_query(db):
 db.add_all([Inquiry(id='a',tenant_id='tenant-demo',raw_message='x',missing_fields=[]),Inquiry(id='b',tenant_id='other',raw_message='x',missing_fields=[])]);db.commit(); assert db.query(Inquiry).filter_by(tenant_id='tenant-demo').count()==1
