import os,requests,uuid
BASE=os.getenv('BASE_URL','http://localhost:8100').rstrip('/');H={'X-Demo-User':'qa','X-Demo-Tenant':'tenant-demo'}
def post(p,j=None): return requests.post(BASE+p,headers=H,json=j or {},timeout=30)
def patch(p,j): return requests.patch(BASE+p,headers=H,json=j,timeout=30)
r=post('/api/v1/messages/inbound',{'external_message_id':'acc-'+uuid.uuid4().hex,'sender_email':'qa@example.com','subject':'quote','body':'Need 1x40HQ from Shanghai to Los Angeles'}); assert r.status_code==200;rj=r.json();assert 'etd' in rj['missing_fields'];inq=rj['inquiry_id']
r2=post('/api/v1/messages/inbound',{'external_message_id':'acc-idem','sender_email':'qa2@example.com','subject':'quote','body':'Need 1x40HQ from Shanghai to Los Angeles'}); r3=post('/api/v1/messages/inbound',{'external_message_id':'acc-idem','sender_email':'qa2@example.com','subject':'quote','body':'changed'}); assert r3.json().get('idempotent') is True
assert patch(f'/api/v1/inquiries/{inq}',{'etd':'2026-09-20'}).status_code==200
assert post(f'/api/v1/inquiries/{inq}/schedules').status_code==200
rfq=post(f'/api/v1/inquiries/{inq}/rfq').json()['rfq']['id']
for i,cost in enumerate([2000,1900]): assert post(f'/api/v1/rfqs/{rfq}/supplier-quotes',{'supplier_name':str(i),'supplier_email':f'supplier{i}@example.com','ocean_freight':cost,'surcharges':100}).status_code==200
q=post(f'/api/v1/rfqs/{rfq}/optimize').json(); qid=q['id']; assert q['sell_amount']>q['cost_amount']
assert post(f'/api/v1/quotes/{qid}/send').status_code==409
assert post(f'/api/v1/quotes/{qid}/review',{'action':'approve'}).status_code==200
assert post(f'/api/v1/quotes/{qid}/send').status_code==200
print('ACCEPTANCE PASS')
