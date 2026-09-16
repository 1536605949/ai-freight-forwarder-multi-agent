import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
import argparse,time,jwt
from app.config import get_settings
ap=argparse.ArgumentParser();ap.add_argument('--user',default='sales001');ap.add_argument('--tenant',default='tenant-demo');ap.add_argument('--roles',default='sales,reviewer');a=ap.parse_args();s=get_settings();print(jwt.encode({'sub':a.user,'tenant_id':a.tenant,'roles':a.roles.split(','),'iss':s.jwt_issuer,'iat':int(time.time()),'exp':int(time.time())+3600},s.jwt_secret,algorithm='HS256'))
