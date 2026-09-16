.PHONY: install install-agent seed test run worker release bootstrap bootstrap-check bootstrap-fast demo acceptance delivery-check help

# Recommended first-time setup: creates the venv, installs deps, init db, seeds data.
# Idempotent - safe to re-run. See scripts/bootstrap_dev.py --help.
bootstrap:
	python scripts/bootstrap_dev.py

# Health check only: touches nothing, verifies the environment is usable.
bootstrap-check:
	python scripts/bootstrap_dev.py --check

# Re-run bootstrap without touching pip (venv already populated).
bootstrap-fast:
	python scripts/bootstrap_dev.py --skip-install

# Assumes an already-active virtualenv.
install:
	pip install -r requirements-dev.txt

# Optional: real LLM orchestration. Not needed for mock mode.
install-agent:
	pip install -r requirements-agent.txt

seed:
	python scripts/seed_demo.py

test:
	pytest -q

run:
	uvicorn app.main:app --reload --port 8100

worker:
	python -m worker.scheduler

demo:
	python scripts/demo_scenario.py

# End-to-end against a RUNNING server (start with `make run` first).
acceptance:
	python scripts/acceptance.py

# Full delivery acceptance: business properties + gates + isolation. Needs a running server.
delivery-check:
	python scripts/delivery_check.py

release:
	python scripts/release_gate.py

help:
	@echo "bootstrap        create venv, install deps, init db, seed (idempotent)"
	@echo "bootstrap-check  verify the environment without changing it"
	@echo "run              start the API on :8100"
	@echo "worker           start the background follow-up worker"
	@echo "test             run the unit/integration suite"
	@echo "acceptance       end-to-end smoke test (needs a running server)"
	@echo "delivery-check   full delivery acceptance (needs a running server)"
	@echo "release          packaging + code-health gate"
