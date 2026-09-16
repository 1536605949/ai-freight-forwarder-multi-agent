import os,requests,json,uuid
BASE=os.getenv('BASE_URL','http://localhost:8100').rstrip('/'); H={'X-Demo-User':'sales001','X-Demo-Tenant':'tenant-demo'}
def call(method,path,**kwargs):
 r=requests.request(method,BASE+path,headers=H,timeout=30,**kwargs); print('\n',method,path,r.status_code); print(json.dumps(r.json(),ensure_ascii=False,indent=2,default=str)); r.raise_for_status(); return r.json()
msg=call('POST','/api/v1/messages/inbound',json={'external_message_id':'demo-'+uuid.uuid4().hex,'sender_email':'buyer@example.com','sender_name':'Alice','subject':'Ocean freight quote','body':'Need 2x40HQ from Shanghai to Los Angeles. Please quote.'})
inq=msg['inquiry_id']; call('PATCH',f'/api/v1/inquiries/{inq}',json={'etd':'2026-09-20','commodity':'furniture','weight_kg':18000}); call('POST',f'/api/v1/inquiries/{inq}/schedules'); call('POST',f'/api/v1/inquiries/{inq}/rates')
r=call('POST',f'/api/v1/inquiries/{inq}/rfq'); rfq=r['rfq']['id']
for i,p in enumerate([(1980,120,14,7,.92),(1900,220,17,14,.80),(2100,80,13,10,.95)]): call('POST',f'/api/v1/rfqs/{rfq}/supplier-quotes',json={'supplier_name':f'Supplier {i+1}','supplier_email':f'supplier{i+1}@example.com','carrier':['MAEU','CMDU','HLCU'][i],'ocean_freight':p[0],'surcharges':p[1],'transit_days':p[2],'free_time_days':p[3],'reliability_score':p[4]})
quote=call('POST',f'/api/v1/rfqs/{rfq}/optimize'); qid=quote['id']; call('POST',f'/api/v1/quotes/{qid}/review',json={'action':'approve','comment':'margin and terms reviewed'}); call('POST',f'/api/v1/quotes/{qid}/send'); b=call('POST','/api/v1/bookings',json={'inquiry_id':inq,'quote_id':qid,'cargo_details':{'commodity':'furniture'}}); call('POST',f"/api/v1/bookings/{b['booking']['id']}/tracking/refresh"); call('GET','/api/v1/dashboard')
