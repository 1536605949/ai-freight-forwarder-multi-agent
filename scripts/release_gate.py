import subprocess,sys,yaml, pathlib
cmds=[[sys.executable,'-m','compileall','-q','app','worker','scripts','tests','migrations'],[sys.executable,'-m','pytest','-q']]
for c in cmds:
 print('$',' '.join(c)); r=subprocess.run(c); 
 if r.returncode: raise SystemExit(r.returncode)
for p in ['deployment/docker-compose.yml','observability/prometheus.yml']:
 yaml.safe_load(pathlib.Path(p).read_text()); print('YAML PASS',p)
assert pathlib.Path('docs/architecture/project2_architecture.png').stat().st_size>100000
print('ARCHITECTURE IMAGE PASS')
print('RELEASE GATE PASS')
