# SPDX-License-Identifier: Apache-2.0
# simhall: the virtual hall. Targets: install, test, lint, fmt, run, intent.
# Uses the active virtualenv if there is one, else ./.venv (python3 -m venv .venv). Override with VENV=...
VENV ?= $(or $(VIRTUAL_ENV),.venv)
PY   := $(VENV)/bin/python
PORT ?= 8900

.PHONY: test lint fmt run install intent

install:
	$(VENV)/bin/pip install -e '.[dev]'

test:
	cd $(CURDIR) && $(PY) -m pytest -q

lint:
	cd $(CURDIR) && $(VENV)/bin/ruff check src tests && $(VENV)/bin/ruff format --check src tests

fmt:
	cd $(CURDIR) && $(VENV)/bin/ruff format src tests && $(VENV)/bin/ruff check --fix src tests

run:
	$(PY) -m simhall serve --golden hall-b --host 127.0.0.1 --port $(PORT)

intent:
	$(PY) -m simhall intent --golden hall-b -o hall-b.yaml
