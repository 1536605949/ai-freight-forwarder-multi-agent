import json, logging, sys
from datetime import datetime, timezone

def configure_logging():
    logging.basicConfig(level=logging.INFO,stream=sys.stdout,format='%(message)s')

def log_event(event:str, **kwargs):
    logging.getLogger('freight').info(json.dumps({'ts':datetime.now(timezone.utc).isoformat(),'event':event,**kwargs},ensure_ascii=False,default=str))
