from locust import HttpUser,task,between
import uuid
class FreightUser(HttpUser):
 wait_time=between(1,3)
 @task(4)
 def inquiry(self): self.client.post('/api/v1/messages/inbound',headers={'X-Demo-User':'load','X-Demo-Tenant':'tenant-demo'},json={'external_message_id':'load-'+uuid.uuid4().hex,'sender_email':'load@example.com','subject':'quote','body':'Need 1x40HQ from Shanghai to Los Angeles'})
 @task(1)
 def dashboard(self): self.client.get('/api/v1/dashboard',headers={'X-Demo-User':'load','X-Demo-Tenant':'tenant-demo'})
