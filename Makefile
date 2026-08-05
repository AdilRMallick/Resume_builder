PY := ./.venv/Scripts/python.exe

.DEFAULT_GOAL := help
.PHONY: help up down logs psql redis dev-setup verify-services install migrate revision test test-py test-go lint fmt fetcher clean nuke

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---- local infrastructure ----------------------------------------------------

up: ## Start Postgres (pgvector) and Redis
	docker compose up -d

down: ## Stop containers, keep volumes
	docker compose down

logs: ## Tail container logs
	docker compose logs -f

psql: ## Open a psql shell
	docker exec -it jme-postgres psql -U jme -d jme

redis: ## Open a redis-cli shell
	docker exec -it jme-redis redis-cli

dev-setup: ## Install Postgres+pgvector and Redis natively (for machines without Docker)
	sudo scripts/dev-setup.sh

verify-services: ## Check Postgres:5433 and Redis:6380 are reachable
	scripts/dev-setup.sh --verify-only

# ---- python ------------------------------------------------------------------

install: ## Create the venv and install the package with dev extras
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

migrate: ## Apply Alembic migrations
	$(PY) -m alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add thing"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

test-py: ## Run the Python test suite
	$(PY) -m pytest -q

lint: ## Ruff + go vet
	$(PY) -m ruff check jme tests
	cd fetcher && go vet ./...

fmt: ## Format Python and Go
	$(PY) -m ruff format jme tests
	cd fetcher && gofmt -w .

# ---- go ----------------------------------------------------------------------

test-go: ## Run the Go test suite (needs Redis and Postgres up)
	cd fetcher && go test ./... -count=1

fetcher: ## Build the fetcher binary
	cd fetcher && go build -o ../bin/fetcher ./cmd/fetcher

# ---- everything --------------------------------------------------------------

test: test-py test-go ## Run both test suites

clean: ## Remove build artifacts
	rm -rf bin .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

nuke: ## Stop containers AND delete their volumes (destroys local data)
	docker compose down -v
