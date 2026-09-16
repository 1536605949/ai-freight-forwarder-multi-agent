from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import get_settings

class Base(DeclarativeBase): pass

s=get_settings()
if s.database_url.startswith('sqlite:///') and ':memory:' not in s.database_url:
    Path(s.database_url.removeprefix('sqlite:///')).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
engine=create_engine(s.database_url, pool_pre_ping=True, connect_args={'check_same_thread':False} if s.database_url.startswith('sqlite') else {})
SessionLocal=sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def get_db():
    db=SessionLocal()
    try: yield db
    finally: db.close()
