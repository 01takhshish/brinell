.DEFAULT_GOAL := help
COMPOSE := docker compose
PY := backend/.venv/bin/python
UV := uv

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## Start Postgres + Redis
	$(COMPOSE) up -d
	@$(COMPOSE) ps

down: ## Stop containers (keeps data)
	$(COMPOSE) down

nuke: ## Stop containers and delete the database volume
	$(COMPOSE) down -v

install: ## Create the backend venv and install dependencies
	cd backend && $(UV) venv --python 3.12 && $(UV) pip install -e ".[dev]"

migrate: ## Apply database migrations
	cd backend && .venv/bin/alembic upgrade head

seed: ## Load the demo project, dataset and suites
	# The slug matches `project:` in examples/support-agent/crucible.yaml. Seeding the default
	# `my-project` instead leaves `crucible push` failing with "project not found", which is a
	# confusing first impression for a command whose entire job is to get you started.
	cd backend && .venv/bin/crucible seed --slug support-agent

api: ## Run the API with reload
	cd backend && .venv/bin/uvicorn crucible.api.app:app --reload --port 8000

worker: ## Run a standalone eval worker
	cd backend && .venv/bin/crucible worker

web: ## Run the frontend
	cd frontend && npm run dev

test: ## Unit tests only (no containers required)
	cd backend && .venv/bin/pytest tests/unit -q

test-all: ## Unit + integration tests (requires `make up`)
	cd backend && .venv/bin/pytest -q

bench: ## Reproducible offline benchmark against the mock provider
	cd backend && .venv/bin/python benchmarks/run_benchmark.py

lint: ## Ruff + mypy
	cd backend && .venv/bin/ruff check src tests benchmarks && .venv/bin/ruff format --check src tests benchmarks && .venv/bin/mypy src benchmarks

fmt: ## Autoformat
	cd backend && .venv/bin/ruff check --fix src tests benchmarks && .venv/bin/ruff format src tests benchmarks

.PHONY: help up down nuke install migrate seed api worker web test test-all bench lint fmt
