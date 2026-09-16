import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from app.db import Base,engine,SessionLocal
from app.models import Tenant
Base.metadata.create_all(engine); db=SessionLocal()
if not db.get(Tenant,'tenant-demo'): db.add(Tenant(id='tenant-demo',name='Demo Freight Forwarder'))
db.commit(); print('Seeded tenant-demo')
