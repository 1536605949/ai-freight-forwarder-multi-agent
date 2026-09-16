from __future__ import annotations
import uuid, httpx
from app.config import get_settings
class BookingProvider:
    async def create(self,payload:dict)->dict: raise NotImplementedError
class MockBookingProvider(BookingProvider):
    async def create(self,payload): return {'bookingReference':'DEMO-'+uuid.uuid4().hex[:10].upper(),'status':'CONFIRMED','payload':payload}
class DCSABookingProvider(BookingProvider):
    def __init__(self):
        s=get_settings(); self.base=(s.dcsa_booking_base_url or '').rstrip('/'); self.token=s.dcsa_booking_token
    async def create(self,payload):
        if not self.base: raise RuntimeError('DCSA_BOOKING_BASE_URL is required')
        h={'Authorization':f'Bearer {self.token}'} if self.token else {}
        async with httpx.AsyncClient(timeout=45) as c:
            r=await c.post(self.base+'/v2/bookings',headers=h,json=payload); r.raise_for_status(); return r.json()
def get_booking_provider(): return DCSABookingProvider() if get_settings().booking_provider=='dcsa' else MockBookingProvider()
