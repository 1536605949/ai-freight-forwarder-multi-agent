from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from datetime import date, timedelta
import httpx
from app.config import get_settings

@dataclass
class ScheduleResult:
    carrier:str; service:str; vessel:str; voyage:str; origin:str; destination:str; etd:str; eta:str; transit_days:int; direct:bool=True; co2e_kg:float|None=None; source:str='mock_dcsa'; payload:dict|None=None

class ScheduleProvider(Protocol):
    async def search(self,origin:str,destination:str,etd:str|None=None)->list[ScheduleResult]: ...

class MockScheduleProvider:
    async def search(self,origin,destination,etd=None):
        base=date.today()+timedelta(days=5)
        carriers=[('MAEU','TP1',13,True,2400),('CMDU','PEX',15,True,2250),('HLCU','PSX',18,False,2100)]
        return [ScheduleResult(c,s,f'{c}-VESSEL',f'{100+i}E',origin,destination,(base+timedelta(days=i*2)).isoformat(),(base+timedelta(days=i*2+t)).isoformat(),t,d,co,'mock_dcsa',{'demo':True}) for i,(c,s,t,d,co) in enumerate(carriers)]

class DCSAScheduleProvider:
    # Generic consumer for a carrier/platform endpoint implementing DCSA Commercial Schedules semantics.
    # Carrier authentication and exact base URL are deployment-specific; no carrier URL is hard-coded.
    def __init__(self):
        s=get_settings(); self.base=(s.dcsa_schedule_base_url or '').rstrip('/'); self.token=s.dcsa_schedule_token
    async def search(self,origin,destination,etd=None):
        if not self.base: raise RuntimeError('DCSA_SCHEDULE_BASE_URL is required')
        headers={'Authorization':f'Bearer {self.token}'} if self.token else {}
        params={'placeOfReceipt':origin,'placeOfDelivery':destination}
        if etd: params['departureStartDate']=etd
        async with httpx.AsyncClient(timeout=30) as c:
            r=await c.get(self.base+'/v1/point-to-point-routes',headers=headers,params=params); r.raise_for_status(); body=r.json()
        # Provider payloads can vary around the standard version; normalize in one place.
        out=[]
        for i,x in enumerate(body if isinstance(body,list) else body.get('items',[])):
            out.append(ScheduleResult(carrier=x.get('carrierServiceCode','UNKNOWN'),service=x.get('carrierServiceName',''),vessel=x.get('vesselName',''),voyage=x.get('carrierVoyageNumber',''),origin=origin,destination=destination,etd=x.get('departureDateTime',etd or ''),eta=x.get('arrivalDateTime',''),transit_days=int(x.get('transitTime',0) or 0),direct=not bool(x.get('isTransshipment',False)),source='dcsa',payload=x))
        return out

def get_schedule_provider()->ScheduleProvider:
    return DCSAScheduleProvider() if get_settings().schedule_provider=='dcsa' else MockScheduleProvider()
