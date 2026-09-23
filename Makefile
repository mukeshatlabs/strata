# Strata: build and run targets. Keep these names exact; CLAUDE.md depends on them.

PYTHON ?= python3
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: setup run test eval reset live

setup:
	$(PYTHON) -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

run:
	rm -f strata.db
	$(PY) -m strata.pipeline bootstrap
	$(PY) -m uvicorn strata.app:app --reload --port 8000

test:
	$(PY) -m pytest

eval:
	$(PY) evals/run_evals.py

reset:
	rm -f strata.db
	$(PY) -m strata.pipeline ingest

live:
	$(PY) -m strata.pipeline live
