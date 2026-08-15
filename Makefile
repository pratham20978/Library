# MCP Hub — development commands (arch §66).
#
# `make` on its own lists everything. Targets are thin: each one is the command
# you would type, so you can always run it directly instead.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# uv owns the environment (it is what `uv.lock` is for). `uv run` syncs before
# it runs, so a stale virtualenv cannot produce a confusing failure.
UV  ?= uv
RUN ?= $(UV) run

# Bind mounts in docker-compose.yml are host directories, so the container has
# to write as you rather than as the image's own user.
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

.PHONY: help install dev test test-all lint format typecheck check inspector \
        docker-up docker-down docker-logs docker-build migrate migration seed health clean

help:  ## List the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------- develop

install:  ## Install the project and its development dependencies
	$(UV) sync --all-extras
	@echo
	@echo "Next: cp .env.example .env, then 'make dev'."

dev:  ## Run the hub with auto-reload at http://localhost:8000 (MCP: /mcp)
	$(RUN) mcp-hub serve --reload

test:  ## Run the test suite
	$(RUN) pytest

test-all:  ## Run the suite with coverage, including the slow tests
	$(RUN) pytest --cov=app --cov-report=term-missing --cov-report=html

lint:  ## Check style and formatting without changing anything
	$(RUN) ruff check app tests scripts
	$(RUN) ruff format --check app tests scripts

format:  ## Apply formatting and the safe lint fixes
	$(RUN) ruff format app tests scripts
	$(RUN) ruff check --fix app tests scripts

typecheck:  ## Type-check under mypy --strict
	$(RUN) mypy app tests scripts

check: lint typecheck test  ## Everything CI runs

# ---------------------------------------------------------------------- state

migrate:  ## Apply database migrations (arch §27)
	$(RUN) alembic upgrade head

migration:  ## Autogenerate a revision: make migration m="add x"
	@test -n "$(m)" || { echo 'Usage: make migration m="what changed"'; exit 2; }
	$(RUN) alembic revision --autogenerate -m "$(m)"

seed:  ## Load config/ into the running state and discover every tool
	$(RUN) mcp-hub sync

health:  ## Probe every integration; exits non-zero if any is unavailable
	$(RUN) mcp-hub health

# --------------------------------------------------------------------- docker

docker-build:  ## Build the hub image
	docker compose build

docker-up:  ## Start hub + postgres + redis in the background
	docker compose up -d --build
	@echo
	@echo "Hub:   http://localhost:$${MCP_HUB_PORT:-8000}"
	@echo "MCP:   http://localhost:$${MCP_HUB_PORT:-8000}/mcp"
	@echo "Logs:  make docker-logs"

docker-down:  ## Stop the stack (named volumes survive)
	docker compose down

docker-logs:  ## Follow the hub's logs
	docker compose logs -f mcp-hub

# --------------------------------------------------------------------- tools

inspector:  ## Open the MCP Inspector against this hub (arch §67)
	@echo "Connect the Inspector to: http://localhost:$${MCP_HUB_PORT:-8000}/mcp"
	@echo "Transport: Streamable HTTP. If the hub requires auth, add an"
	@echo "Authorization: Bearer <token> header from 'mcp-hub token issue'."
	npx -y @modelcontextprotocol/inspector

clean:  ## Remove caches and build artifacts (never runtime/ or .env)
	rm -rf .mypy_cache .pytest_cache .ruff_cache htmlcov .coverage dist build
	find . -name '__pycache__' -type d -prune -not -path './.venv/*' -exec rm -rf {} +
