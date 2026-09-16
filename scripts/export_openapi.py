import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from app.main import app
p=Path('openapi/freight_agent.openapi.json');p.parent.mkdir(exist_ok=True);p.write_text(json.dumps(app.openapi(),ensure_ascii=False,indent=2),encoding='utf-8');print('Wrote',p)
