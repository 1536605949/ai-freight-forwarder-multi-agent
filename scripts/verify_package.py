from pathlib import Path
required=['README.md','docs/architecture/project2_architecture.png','app/main.py','app/agents/runtime.py','app/services/pricing.py','deployment/docker-compose.yml','evaluation/golden_dataset.jsonl']
for x in required: assert Path(x).exists(),x
print('PACKAGE PASS',len(required),'required artifacts')
