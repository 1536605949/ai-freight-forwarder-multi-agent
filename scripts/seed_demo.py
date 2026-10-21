import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from app.db import engine,SessionLocal
from app.models import Tenant
from app.schema_guard import ensure_schema
# Refuses instead of creating tables: `create_all` would build a schema with no
# alembic_version row, which `alembic upgrade head` then cannot adopt.
ensure_schema(engine); db=SessionLocal()
if not db.get(Tenant,'tenant-demo'): db.add(Tenant(id='tenant-demo',name='Demo Freight Forwarder'))
db.commit(); print('Seeded tenant-demo')
