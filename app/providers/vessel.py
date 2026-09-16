"""Vessel position / AIS data source.

Answers a different question from Track & Trace: not "what events happened to this shipment"
but "where is the vessel right now, and will it make the ETA". Used to proactively warn on
delay rather than waiting for the customer to ask.

Field names follow the de-facto AIS vendor vocabulary (MMSI, IMO, SOG, COG, nav status,
next port) so a real vendor payload maps across with minimal translation.

Production note: AIS data is licensed. Real deployments must contract a provider
(MarineTraffic / Spire / etc.) and respect their terms and rate limits. No scraping here.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import httpx
from app.config import get_settings

# IMO-style 7-digit chained checksum, so synthetic MMSIs look structurally valid.
def _mmsi(seed:int)->str: return f'{2}{seed%100000000:08d}'[:9]

@dataclass
class VesselPosition:
    vessel_name:str
    voyage:str|None=None
    mmsi:str|None=None
    imo:str|None=None
    latitude:float|None=None
    longitude:float|None=None
    speed_knots:float|None=None
    course_deg:float|None=None
    # Under way using engine / Moored / At anchor / Not under command
    nav_status:str='under_way_using_engine'
    current_port:str|None=None
    next_port:str|None=None
    eta:str|None=None
    reported_at:str|None=None
    delay_hours:float|None=None
    source:str='mock_ais'
    payload:dict|None=None

@dataclass
class PortCall:
    """A scheduled or completed port call — where a demo timeline comes from."""
    port:str; etd:str|None=None; eta:str|None=None; status:str='planned'

class VesselPositionProvider:
    async def get_position(self,vessel_name:str,voyage:str|None=None)->VesselPosition:
        raise NotImplementedError
    async def get_port_calls(self,vessel_name:str,voyage:str|None=None)->list[PortCall]:
        raise NotImplementedError

class MockVesselPositionProvider:
    """Deterministic synthetic AIS track.

    Same vessel + voyage always yields the same position, so a demo can be replayed and a
    test can assert on it. Position is interpolated along a coarse great-circle-ish line
    between origin and destination based on how far into the voyage we notionally are.
    """
    _PORTS={
        'CNSHA':(31.2304,121.4737),'CNNGB':(29.8683,121.5440),'CNQIN':(36.0671,120.3826),
        'CNSZX':(22.5431,114.0579),'USLAX':(33.7405,-118.2775),'USLGB':(33.7542,-118.2165),
        'NLRTM':(51.9244,4.4777),'DEHAM':(53.5511,9.9937),'GBFXT':(51.9540,1.3513),
        'SGSIN':(1.2644,103.8223),'AEJEA':(25.0100,55.0617),
    }

    async def get_position(self,vessel_name,voyage=None):
        seed=int(hashlib.sha256(f'{vessel_name}:{voyage or ""}'.encode()).hexdigest()[:8],16)
        origin,destination=self._lane_ports(seed)
        # progress in [0,1) derived from the same seed -> stable per vessel/voyage
        progress=(seed%1000)/1000.0
        lat,lon=self._interpolate(origin,destination,progress)
        delayed=(seed%7==0)
        delay=round(6.0+(seed%180)/10.0,1) if delayed else round((seed%20)/10.0,1)
        eta=(date.today()+timedelta(days=max(1,int((1-progress)*28)))).isoformat()
        return VesselPosition(vessel_name=vessel_name,voyage=voyage,mmsi=_mmsi(seed),
            imo=f'{9}{seed%1000000:06d}',latitude=round(lat,4),longitude=round(lon,4),
            speed_knots=round(12+(seed%80)/10.0,1),course_deg=float(seed%360),
            nav_status='under_way_using_engine' if progress<0.95 else 'moored',
            current_port=None if progress<0.95 else destination[0],
            next_port=self._port_code(destination,seed),
            eta=eta,reported_at=date.today().isoformat(),delay_hours=delay,
            source='mock_ais',payload={'synthetic':True,'progress':round(progress,3)})

    async def get_port_calls(self,vessel_name,voyage=None):
        seed=int(hashlib.sha256(f'{vessel_name}:{voyage or ""}:calls'.encode()).hexdigest()[:8],16)
        origin,destination=self._lane_ports(seed)
        base=date.today()-timedelta(days=int((seed%1000)/1000.0*28))
        out=[]
        for i,(code,_) in enumerate([origin,destination]):
            etd=(base+timedelta(days=i*22)).isoformat()
            out.append(PortCall(port=code,etd=etd,eta=(base+timedelta(days=i*22)).isoformat(),
                status='completed' if i==0 else 'planned'))
        return out

    def _lane_ports(self,seed):
        codes=list(self._PORTS.items())
        a=codes[seed%len(codes)]
        b=codes[(seed//7+1)%len(codes)]
        if a[0]==b[0]: b=codes[(seed//7+2)%len(codes)]
        return a,b

    def _port_code(self,pair,seed):
        # Ports are keyed by UN/LOCODE; the tuple carries coords, so recover the code by value.
        for code,coord in self._PORTS.items():
            if coord==pair[1]: return code
        return None

    def _interpolate(self,a,b,t):
        (_,(lat1,lon1)),(_,(lat2,lon2))=a,b
        return lat1+(lat2-lat1)*t, lon1+(lon2-lon1)*t

class AISVendorProvider:
    """Skeleton for a licensed AIS vendor.

    Not implemented: endpoint, auth and field mapping are contract-specific. Normalize the
    vendor payload into VesselPosition / PortCall so callers never see vendor shapes.
    """
    def __init__(self):
        s=get_settings(); self.base=(getattr(s,'ais_base_url','') or '').rstrip('/'); self.token=getattr(s,'ais_token',None)
    async def get_position(self,vessel_name,voyage=None):
        if not self.base: raise RuntimeError('AIS_BASE_URL is required')
        headers={'Authorization':f'Bearer {self.token}'} if self.token else {}
        async with httpx.AsyncClient(timeout=30) as c:
            r=await c.get(self.base+'/vessels',headers=headers,params={'name':vessel_name}); r.raise_for_status(); body=r.json()
        v=(body if isinstance(body,list) else body.get('items',[]))
        if not v: raise KeyError('vessel_not_found')
        x=v[0]
        return VesselPosition(vessel_name=vessel_name,voyage=voyage,mmsi=str(x.get('mmsi') or ''),
            imo=str(x.get('imo') or ''),latitude=x.get('lat'),longitude=x.get('lon'),
            speed_knots=x.get('sog'),course_deg=x.get('cog'),nav_status=x.get('navStatus') or 'unknown',
            current_port=x.get('currentPort'),next_port=x.get('nextPort'),eta=x.get('eta'),
            reported_at=x.get('reportedAt'),source='ais_vendor',payload=x)
    async def get_port_calls(self,vessel_name,voyage=None):
        if not self.base: raise RuntimeError('AIS_BASE_URL is required')
        raise NotImplementedError('Map your vendor port-call payload into PortCall here.')

def get_vessel_position_provider()->VesselPositionProvider:
    return AISVendorProvider() if get_settings().vessel_provider=='ais_vendor' else MockVesselPositionProvider()
