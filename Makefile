.PHONY: help install dev lint type-check test test-cov eval seed run-api run-ui clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	uv sync

dev: ## Install all dependencies (dev + eval)
	uv sync --all-extras

lint: ## Run ruff linter
	uv run ruff check src/ tests/

format: ## Auto-format code
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

type-check: ## Run mypy type checker
	uv run mypy src/

test: ## Run all tests
	uv run pytest tests/ -v

test-cov: ## Run tests with coverage report
	uv run pytest tests/ -v --cov=src --cov-report=html --cov-report=term

eval: ## Run evaluation harness
	uv run python eval/run_evals.py

seed: ## Seed the Olist database into DuckDB
	uv run python data/seed_olist.py

run-api: ## Start FastAPI server and Clean Web UI (Vue.js 3 + Bootstrap 5)
	uv run uvicorn src.api.server:app --reload --port 8000

run-web: ## Start Clean Web UI (Vue.js 3 + Bootstrap 5) on port 8000
	uv run uvicorn src.api.server:app --reload --port 8000

run-ui: ## Start Streamlit UI
	uv run streamlit run ui/app.py --server.port 8501

red-team: ## Run security red-team test suite
	uv run pytest tests/test_red_team.py -v --tb=long

clean: ## Remove generated artifacts
	rm -rf outputs/ data/analytics.duckdb data/*.parquet
	rm -rf __pycache__ .pytest_cache .mypy_cache .ruff_cache htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
