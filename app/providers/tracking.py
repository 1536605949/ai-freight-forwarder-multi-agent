from __future__ import annotations
import httpx
from app.config import get_settings
class TrackingProvider:
    async def get_events(self,reference:str)->list[dict]: raise NotImplementedError
class MockTrackingProvider(TrackingProvider):
    async def get_events(self,reference): return [{'eventCode':'LOAD','eventTime':'2026-09-20T08:00:00Z','location':'CNSHA','description':'Loaded on vessel'}]
class DCSATrackingProvider(TrackingProvider):
    def __init__(self):
        s=get_settings(); self.base=(s.dcsa_tracking_base_url or '').rstrip('/'); self.token=s.dcsa_tracking_token
    async def get_events(self,reference):
        if not self.base: raise RuntimeError('DCSA_TRACKING_BASE_URL required')
        h={'Authorization':f'Bearer {self.token}'} if self.token else {}
        async with httpx.AsyncClient(timeout=30) as c:
            r=await c.get(self.base+'/v2/events',headers=h,params={'documentReference':reference}); r.raise_for_status(); data=r.json()
        return data if isinstance(data,list) else data.get('events',[])
def get_tracking_provider(): return DCSATrackingProvider() if get_settings().tracking_provider=='dcsa' else MockTrackingProvider()
