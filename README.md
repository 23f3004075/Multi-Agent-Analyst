# Enterprise Multi-Agent SQL Analyst & Autonomous BI Engine

An enterprise-grade Natural-Language-to-SQL (NL-to-SQL) and autonomous Business Intelligence engine featuring adaptive model routing, AST-verified sandboxed execution, self-healing query correction, and multi-modal report generation.

---

## Key Features

- **Adaptive Model Routing**: Dynamically classifies query complexity, routing 70%+ of queries to cost-effective Small Language Models (Tier 1 SLM) and escalating multi-join, windowed, or ambiguous queries to Frontier models (Tier 2).
- **Deterministic 3-Layer Security**:
  - *Layer 1 (Database Core)*: DuckDB locked with `read_only=True`, `enable_external_access=false`, and `lock_configuration=true`.
  - *Layer 2 (AST Sandboxing)*: Strict sqlglot AST allowlist verifying queries against approved node types, blocking mutations, filesystem reads, recursion bombs, and parser-differential string escape injections.
  - *Layer 3 (Prompt Pre-Filters)*: Regex and jailbreak detection sanitizing incoming user queries.
- **Governed Semantic Layer**: Centralized business metric catalog (`revenue`, `aov`, `delivery_delay_days`, `review_score`) ensuring standardized formulas and eliminating query hallucination.
- **Self-Healing State Machine**: Cyclic LangGraph Directed Acyclic Graph (DAG) with error context propagation and automated tier escalation on failure.
- **Dual User Interfaces**:
  - **Clean Enterprise Web UI** (`web/`): Fast, responsive interface built with HTML5, Bootstrap 5.3, Vue.js 3, and Plotly.js (strictly no emojis).
  - **Streamlit Analyst Dashboard** (`ui/app.py`): Full-featured dashboard with schema browser and execution traces.
- **Multi-Modal Export**: Hardened PDF (WeasyPrint isolated URL fetcher) and Excel (formula injection escaping) reports.

---

## Documentation Links

- **[Startup & Usage Guide](STARTUP_GUIDE.md)**: Detailed step-by-step instructions for installation, configuration, database seeding, launching the Web UI, and running tests.
- **[Full Technical Architecture](ARCHITECTURE.md)**: Exhaustive module-by-module breakdown explaining every file, class, function, security vector, and data flow across the system.

---

## Quick Start

### 1. Install Dependencies
```bash
uv sync
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env and supply your GEMINI_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY
```

### 3. Seed Database
```bash
uv run python data/seed_olist.py
```

### 4. Run the Clean Web UI (Vue.js 3 + Bootstrap 5)
```bash
uv run uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --reload
```
Open [http://localhost:8000](http://localhost:8000) in your browser.

### 5. (Alternative) Run Streamlit Dashboard
```bash
uv run streamlit run ui/app.py
```
Open [http://localhost:8501](http://localhost:8501) in your browser.

### 6. Run Automated Test Suite
```bash
uv run python -m pytest tests/ -v
```
*118 passing tests covering unit validation, 50 red-team attack payloads (0 bypasses), sandbox security, and state machine orchestration.*
