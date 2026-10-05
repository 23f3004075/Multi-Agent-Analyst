# Enterprise Multi-Agent SQL Analyst — Startup & Usage Guide

This guide walks you through setting up, configuring, launching, and testing the Enterprise Multi-Agent SQL Analyst platform.

---

## 1. System Requirements & Prerequisites

- **Python**: Version 3.11 or higher (Python 3.11, 3.12, or 3.13 supported).
- **Package Manager**: [`uv`](https://github.com/astral-sh/uv) (recommended) or standard `pip`.
- **Operating System**: Windows, Linux, or macOS.
- **Disk Space**: ~300 MB for Python environment and Olist analytical database.
- **Memory**: 4 GB RAM minimum (8 GB recommended for sentence embeddings).

---

## 2. Environment Setup

### Step 2.1: Clone & Navigate to Project

```bash
cd "d:\PERSONAL PROJECTS\Multi_agent_analyst\enterprise-sql-agent"
```

### Step 2.2: Install Dependencies

Using `uv` (fastest):
```bash
uv sync
```

Alternatively, using standard virtual environment and pip:
```bash
python -m venv .venv
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

pip install -e .
```

### Step 2.3: Configure API Keys (`.env`)

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Open `.env` and configure your API key(s). The system uses LiteLLM and dynamically routes between Tier 1 (SLM / fast model) and Tier 2 (Frontier / reasoning model).

```ini
# Environment
ENVIRONMENT=production
LOG_LEVEL=INFO

# Database Path
DATABASE_PATH=data/analytics.duckdb

# LLM Keys (Set at least one provider)
# Google Gemini (Default Tier 1 & Tier 2)
GEMINI_API_KEY=your_gemini_api_key_here

# Anthropic (Optional)
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# OpenAI (Optional)
OPENAI_API_KEY=your_openai_api_key_here

# Model Routing Configuration
TIER_1_MODEL=gemini/gemini-1.5-flash
TIER_2_MODEL=gemini/gemini-1.5-pro

# Execution Sandbox Limits
MAX_EXECUTION_TIME_SECONDS=30
MAX_MEMORY_LIMIT=2GB
MAX_RETURN_ROWS=5000
```

---

## 3. Database Initialization (Seeding Olist)

The project includes an automated database seeder that downloads the Brazilian E-Commerce public dataset (Olist) and builds an optimized, indexed DuckDB database with 9 relational tables and 1.5M+ rows.

Run the seeder:
```bash
uv run python data/seed_olist.py
```

### Seeded Schema Overview:
1. `customers` (~99k rows): Customer ID, zip prefix, city, state.
2. `orders` (~99k rows): Order status, purchase/approved/delivered timestamps.
3. `order_items` (~113k rows): Product ID, seller ID, item price, freight value.
4. `order_payments` (~104k rows): Payment type (credit card, boleto, voucher), installments, sequential amount.
5. `order_reviews` (~99k rows): Review score (1-5), review title, comments, response timestamps.
6. `products` (~33k rows): Product category, weight, dimensions.
7. `sellers` (~3k rows): Seller zip prefix, city, state.
8. `product_category` (71 rows): Portuguese to English category translation lookup.
9. `geolocation` (~1M rows): Zip coordinates, latitude, longitude, city, state.

---

## 4. Starting the User Interfaces

The platform provides two frontend interfaces depending on your workflow requirements:

### Option A: Clean Enterprise Web UI (Vue.js 3 + Bootstrap 5, Strictly No Emojis)

A responsive, corporate web application built with HTML5, Bootstrap 5.3, Vue.js 3, and Plotly.js. Features zero emojis, metric tiles, interactive charting, tabular preview, and downloadable reports.

Start the FastAPI application server:
```bash
uv run uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --reload
```

Open your browser to:
```
http://localhost:8000
```

#### Key Capabilities of the Web UI:
- **Natural-Language Query Bar**: Input business questions in plain English with sample query chips.
- **System Metrics Ribbon**: Displays active Model Tier, Router Confidence %, Total Latency (ms), Execution Cost ($), and Security Status.
- **Multi-Tab Workspace**:
  - *Executive Summary*: Formatted business findings, anomaly warnings, and conclusions.
  - *Interactive Chart*: Zoomable, hoverable Plotly visualization.
  - *Data Preview*: Tabular view of query output with truncation indicators.
  - *Generated SQL & Audit*: View the AST-verified SQL statement and healing log.
  - *Export Reports*: Direct download buttons for generated PDF and Excel workbooks.

---

### Option B: Streamlit Analyst Dashboard

For data teams preferring Streamlit's dashboard workflow:
```bash
uv run streamlit run ui/app.py
```

Open your browser to:
```
http://localhost:8501
```

---

## 5. Programmatic Python API

You can execute queries directly inside Python scripts, Jupyter notebooks, or scheduled background tasks:

```python
from src.agents.graph import run_query

# Execute query through full 12-node pipeline
result = run_query("What are the top 5 product categories by revenue in 2018?")

print("Status:", "Passed" if result["guardrail_passed"] else "Blocked")
print("Model Used:", result["model_used"])
print("Generated SQL:\n", result["generated_sql"])
print("Summary:\n", result["final_response"])
print("Latency:", result["total_latency_ms"], "ms")
print("Cost: $", result["total_cost_usd"])
```

---

## 6. Running the Automated Test Suite

The platform includes an automated regression test suite covering unit operations, sandboxed execution, red-team bypass resistance, and state graph orchestration.

Run all tests:
```bash
uv run python -m pytest tests/ -v
```

### Running Specific Test Modules:

1. **Security Red-Team Regression (50 Payloads)**:
   ```bash
   uv run python -m pytest tests/test_red_team.py -v
   ```
   *Verifies 100% block rate (0 bypasses) across filesystem reads, command injection, CTE recursion bombs, tautologies, and parser differentials.*

2. **AST Allowlist Validator Tests**:
   ```bash
   uv run python -m pytest tests/test_ast_checker.py -v
   ```

3. **DuckDB 3-Layer Sandbox Security**:
   ```bash
   uv run python -m pytest tests/test_sandbox_execution.py -v
   ```

4. **LangGraph State Machine Integration**:
   ```bash
   uv run python -m pytest tests/test_agent_graph.py -v
   ```

---

## 7. REST API Endpoints

When running `src/api/server.py`, the following REST endpoints are exposed:

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Serves the HTML5 + Bootstrap 5 + Vue.js frontend |
| `GET` | `/api/health` | Returns database status, path, and active table catalog |
| `POST` | `/api/query` | Executes natural-language query; returns SQL, analysis, charts, and metrics |
| `GET` | `/api/download` | Secure endpoint to download compiled PDF or Excel reports |
| `GET` | `/docs` | Interactive Swagger UI API documentation |

### Example REST Call (cURL):

```bash
curl -X POST "http://localhost:8000/api/query" \
     -H "Content-Type: application/json" \
     -d '{"query": "Show monthly order count for 2017", "max_retries": 2}'
```

---

## 8. Troubleshooting & FAQ

### Issue: "Database not found"
**Cause**: The database has not been seeded yet.  
**Resolution**: Run `uv run python data/seed_olist.py`. Ensure `data/analytics.duckdb` is generated.

### Issue: "Windows file lock error during seeding"
**Cause**: File handles held by other applications or prior runs.  
**Resolution**: The seeder now includes automatic lock isolation. If a lock persists, ensure no other DuckDB connections (e.g. DBeaver or Python shell) are actively holding `data/analytics.duckdb`.

### Issue: "Hugging Face symlink warning on Windows"
**Cause**: Windows non-admin accounts do not permit symlinks by default.  
**Resolution**: This warning is harmless; Hugging Face falls back to copying the weights. To eliminate the warning, set environment variable `HF_HUB_DISABLE_SYMLINKS_WARNING=1`.
