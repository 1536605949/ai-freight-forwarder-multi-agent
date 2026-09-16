import os
os.environ['DATABASE_URL']='sqlite:///:memory:'
os.environ['AUTH_MODE']='dev'; os.environ['AGENT_PROVIDER']='mock'; os.environ['EMAIL_PROVIDER']='mock'; os.environ['SCHEDULE_PROVIDER']='mock'; os.environ['BOOKING_PROVIDER']='mock'
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import Tenant
@pytest.fixture
def db():
 e=create_engine('sqlite:///:memory:'); Base.metadata.create_all(e); S=sessionmaker(bind=e,expire_on_commit=False); d=S(); d.add(Tenant(id='tenant-demo',name='Demo')); d.commit(); yield d; d.close()
