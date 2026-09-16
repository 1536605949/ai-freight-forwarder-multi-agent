from dataclasses import dataclass
from fastapi import Header, HTTPException, Depends
import jwt
from app.config import get_settings

@dataclass
class Principal:
    user_id:str; tenant_id:str; roles:list[str]

def current_principal(authorization:str|None=Header(default=None), x_demo_user:str|None=Header(default=None), x_demo_tenant:str|None=Header(default=None)) -> Principal:
    s=get_settings()
    if s.auth_mode=='dev':
        return Principal(user_id=x_demo_user or 'sales001', tenant_id=x_demo_tenant or 'tenant-demo', roles=['sales','reviewer','admin'])
    if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'missing bearer token')
    try:
        p=jwt.decode(authorization.split(' ',1)[1],s.jwt_secret,algorithms=['HS256'],issuer=s.jwt_issuer)
        return Principal(user_id=p['sub'],tenant_id=p['tenant_id'],roles=p.get('roles',[]))
    except Exception as e: raise HTTPException(401,'invalid token') from e

def require_role(role:str):
    def dep(p:Principal=Depends(current_principal)):
        if role not in p.roles and 'admin' not in p.roles: raise HTTPException(403,f'role {role} required')
        return p
    return dep
