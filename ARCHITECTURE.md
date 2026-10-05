# Enterprise Multi-Agent SQL Analyst — Technical Architecture & Codebase Guide

This document provides an exhaustive architectural reference and module-by-module breakdown of the **Enterprise Multi-Agent SQL Analyst & Autonomous BI Engine**.

---

## 1. System Overview & Core Philosophy

The platform is designed to bridge the gap between unstructured business questions and governed, deterministic data analytics. Traditional LLM-to-SQL systems suffer from three primary enterprise failure modes:
1. **Security Vulnerabilities**: Direct execution of unverified LLM output exposes the database to data exfiltration, arbitrary code execution, denial-of-service (DoS), and prompt injection.
2. **High Latency & Prohibitive Costs**: Routing every question to large frontier models (e.g. GPT-4o, Claude 3.5 Sonnet) introduces excessive latency (3-10s) and high token costs for simple aggregation queries.
3. **Semantic Drift & Query Hallucination**: LLMs lack institutional knowledge of specific business metrics (e.g., how "Average Order Value" or "Active Customer" is defined), leading to plausible but mathematically incorrect SQL queries.

### Guiding Principles of the Architecture:
- **Defense-in-Depth Security**: Inverting traditional blacklists to an **explicit AST allowlist**, reinforced by a 3-layer database sandbox boundary.
- **Adaptive Model Routing**: Dynamically evaluating query complexity to route 70%+ of queries to fast, cost-effective Small Language Models (SLMs / Tier 1), reserving Frontier models (Tier 2) for multi-join, windowed, or ambiguous queries.
- **Semantic Layer as Code**: Centralizing business metrics, synonyms, and formulas to guarantee mathematical consistency across all generated SQL.
- **Self-Healing State Machine**: Using a cyclic **LangGraph** Directed Acyclic Graph (DAG) with error escalation, preventing catastrophic failure by self-correcting syntax and logical errors.
- **Parquet-Based Data Isolation**: Raw data never flows through agent prompt memory; query results are persisted in local Parquet files, passing lightweight metadata references to downstream visualization and reporting nodes.

---

## 2. High-Level Architecture Diagram

```
                                  [User Business Question]
                                             │
                                             ▼
                             ┌───────────────────────────────┐
                             │    1. Input Guardrail Node    │
                             │   (Regex & Security Filter)   │
                             └───────────────────────────────┘
                                     │               │
                            [Clean]  │               │ [Malicious / Injection]
                                     ▼               ▼
                        ┌──────────────────┐   ┌───────────────────────────┐
                        │  2. Router Node  │   │ 10. Terminal Reject Exit  │
                        └──────────────────┘   └───────────────────────────┘
                                 │
           ┌─────────────────────┴─────────────────────┐
           ▼                                           ▼
┌──────────────────────┐                   ┌────────────────────────┐
│   Schema Linker      │                   │     Semantic Layer     │
│ (Sentence Embedding) │                   │  (Metric Governance)   │
└──────────────────────┘                   └────────────────────────┘
           │                                           │
           └─────────────────────┬─────────────────────┘
                                 ▼
                    ┌─────────────────────────┐
                    │ Complexity Classification│
                    │  (Tier 1 SLM vs Tier 2) │
                    └─────────────────────────┘
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
       ┌───────────────────┐           ┌───────────────────┐
       │ 3. SQL Generator  │           │ 3. SQL Generator  │
       │    (Tier 1 SLM)   │           │ (Tier 2 Frontier) │
       └───────────────────┘           └───────────────────┘
                 │                               │
                 └───────────────┬───────────────┘
                                 ▼
                     ┌───────────────────────┐
                     │ 4. AST Validator Node │ ── [Unsafe AST] ──┐
                     │ (sqlglot Allowlist)   │                   │
                     └───────────────────────┘                   │
                                 │ [Valid AST]                   │
                                 ▼                               │
                     ┌───────────────────────┐                   │
                     │ 5. Execution Sandbox  │ ── [Query Error] ─┤
                     │ (DuckDB 3-Layer Lock) │                   │
                     └───────────────────────┘                   │
                                 │ [Success - Parquet]           │
                                 ▼                               ▼
                     ┌───────────────────────┐        ┌───────────────────┐
                     │ 6. Analysis Node      │        │ 9. Self-Heal Node │
                     │ (Sanity & Anomaly)    │        │ (Escalate Tier 1  │
                     └───────────────────────┘        │   to Tier 2)      │
                                 │                    └───────────────────┘
                                 ▼                              │
                     ┌───────────────────────┐          (If Retries > Max)
                     │ 7. Visualizer Node    │                  ▼
                     │ (Deterministic Chart) │        ┌───────────────────┐
                     └───────────────────────┘        │ 11. Terminal Error│
                                 │                    └───────────────────┘
                                 ▼
                     ┌───────────────────────┐
                     │ 8. Report Node        │
                     │ (Secure PDF & Excel)  │
                     └───────────────────────┘
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │ 12. Final Response    │
                     │  (Web UI / Dashboard) │
                     └───────────────────────┘
```

---

## 3. Detailed Directory & Module Map

```
enterprise-sql-agent/
├── data/
│   ├── analytics.duckdb              # Seeded Olist DuckDB database (9 tables, 1.5M+ rows)
│   └── seed_olist.py                 # Automated dataset downloader, schema builder & indexer
├── src/
│   ├── __init__.py
│   ├── config.py                     # Pydantic Settings configuration management
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── state.py                  # TypedDict defining complete LangGraph AgentState
│   │   ├── graph.py                  # 12-node StateGraph assembly, conditional edges, runner
│   │   └── nodes/
│   │       ├── __init__.py
│   │       ├── guardrail_node.py     # Layer 3 input validation & prompt injection defense
│   │       ├── router_node.py        # Schema linking, metric retrieval & tier assignment
│   │       ├── sql_generator.py      # Dual-tier LLM generation & self-healing templates
│   │       ├── ast_validator.py      # Sqlglot AST traversal & allowlist verification
│   │       ├── execution_node.py     # Sandboxed DuckDB execution & parquet reference storage
│   │       ├── heal_node.py          # Retry increment, error aggregation & tier escalation
│   │       ├── analysis_node.py      # Statistical sanity checks & executive commentary
│   │       ├── visualizer_node.py    # Rule-based / LLM Plotly figure synthesis
│   │       ├── report_node.py        # Sandboxed PDF (WeasyPrint) & Excel (XlsxWriter) builders
│   │       └── terminal_error_node.py# Graceful circuit-breaking on exhausted retries
│   ├── api/
│   │   ├── __init__.py
│   │   └── server.py                 # FastAPI backend, REST endpoints & static UI mount
│   ├── database/
│   │   ├── __init__.py
│   │   ├── connection.py             # 3-layer DuckDB connection factory & configuration locks
│   │   ├── executor.py               # Sandboxed execution engine, timeout & memory management
│   │   └── schema_inspector.py       # DDL extraction, column typing & relationship discovery
│   ├── guardrails/
│   │   ├── __init__.py
│   │   ├── input_sanitizer.py        # Regex pre-filter against prompt injection & SQL tokens
│   │   └── sql_ast_checker.py        # AST allowlist, blocked functions & attack mitigations
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py                 # LiteLLM wrapper with token tracking & latency measurement
│   │   └── prompts/
│   │       ├── __init__.py
│   │       └── sql_generation.py     # Zero-shot, few-shot, and self-healing system prompts
│   ├── reports/                      # Output directory for generated PDF and Excel workbooks
│   ├── router/
│   │   ├── __init__.py
│   │   └── router.py                 # Complexity classification engine (Heuristics + Embeddings)
│   ├── schema/
│   │   ├── __init__.py
│   │   ├── linker.py                 # Sentence-transformers semantic schema linking
│   │   └── semantic_layer.py         # Standardized metric formulas, definitions & mappings
│   ├── security/
│   │   └── red_team_payloads.json    # 50 adversarial attack vectors for regression testing
│   └── visualization/
│       └── __init__.py
├── tests/
│   ├── __init__.py
│   ├── test_ast_checker.py           # Unit tests for AST allowlist validation (39 tests)
│   ├── test_red_team.py              # Red-team regression suite (60 tests, 50/50 payload blocks)
│   ├── test_sandbox_execution.py     # Sandbox security & execution engine tests (11 tests)
│   └── test_agent_graph.py           # LangGraph state machine & integration tests (8 tests)
├── ui/
│   ├── app.py                        # Streamlit multi-tab analytics dashboard
│   └── static/
├── web/                              # Clean HTML + CSS + Vue.js / Bootstrap 5 frontend (no emoji)
│   ├── index.html                    # Responsive single-page layout
│   ├── style.css                     # Corporate CSS design system
│   └── app.js                        # Vue.js 3 reactivity & Plotly rendering
├── pyproject.toml                    # UV / Pip dependency manifest
├── Makefile                          # Task automation (test, seed, run-ui, lint)
├── STARTUP_GUIDE.md                  # Comprehensive setup & operational guide
└── ARCHITECTURE.md                   # This architectural document
```

---

## 4. Deep-Dive Module Breakdown

### 4.1. Core Configuration (`src/config.py`)
- **Class `Settings`**: Built using `pydantic-settings`. Reads environment variables from `.env` or system environment.
  - `database_path`: Path to the DuckDB file (`data/analytics.duckdb`).
  - `environment`: `development` | `staging` | `production`.
  - `tier_1_model` (e.g. `gemini/gemini-1.5-flash`): Fast SLM for straightforward queries.
  - `tier_2_model` (e.g. `gemini/gemini-1.5-pro`): High-capacity reasoning frontier model.
  - `max_execution_time_seconds`: Hard execution timeout (default: 30s).
  - `max_memory_limit`: Memory ceiling enforced on DuckDB connection (default: `2GB`).
  - `max_return_rows`: Max rows returned to prevent client memory exhaustion (default: 5,000).
- **Function `get_settings()`**: Cached singleton provider using `@lru_cache()`.

---

### 4.2. Database & Sandboxed Execution (`src/database/`)

#### `src/database/connection.py`
Implements the **first line of physical security** directly in DuckDB's C++ core:
- **`create_secure_connection(settings: Settings) -> duckdb.DuckDBPyConnection`**:
  Configures the connection with three mandatory enterprise flags:
  1. `read_only=True`: Completely prevents data modification (INSERT, UPDATE, DELETE, DROP, ALTER) at the filesystem storage engine level.
  2. `config={"enable_external_access": "false"}`: Disables all DuckDB functions that touch local disk or network sockets (`read_csv`, `read_parquet`, `glob`, `httpfs`, `INSTALL`, `LOAD`).
  3. `config={"lock_configuration": "true"}`: Permanently locks settings, preventing SQL queries from executing `SET enable_external_access = true` or `RESET`.
- **`ConnectionManager`**: Context manager handling clean acquisition and termination of DuckDB connections without thread leaks.

#### `src/database/executor.py`
Manages query execution, lifecycle timeouts, memory constraints, and analytical profiling:
- **Dataclass `QueryResult`**: Contains execution metadata:
  - `result_id`: UUID4 query identifier.
  - `parquet_path`: Absolute path where query results are persisted in columnar format.
  - `schema`: Column names mapped to DuckDB types.
  - `row_count`: Number of rows returned.
  - `execution_time_ms`: Exact query runtime.
  - `truncated`: Boolean indicating whether rows exceeded `max_rows`.
  - `summary_stats`: Precomputed numeric and categorical summary statistics.
- **Method `execute(sql, conn)`**:
  - Implements query timeout using a background timer thread invoking `conn.interrupt()`.
  - Automatically wraps queries to detect truncation: fetches up to `max_rows + 1`.
  - Exports result set to a temporary Parquet file (`data/results/{uuid}.parquet`) using `pyarrow`.
  - Computes statistical summaries (min, max, mean, std, null count, distinct count) for numeric columns, and top 5 frequencies for categorical columns.

#### `src/database/schema_inspector.py`
Discovers and formats database catalog metadata:
- **Class `SchemaInspector`**:
  - Inspects DuckDB's `information_schema.tables` and `information_schema.columns`.
  - Generates clean, standardized DDL representations used by prompt templates.
  - Discovers primary/foreign key relationships across tables (e.g. `orders.customer_id` $\to$ `customers.customer_id`).

---

### 4.3. Deterministic Security & AST Sandboxing (`src/guardrails/`)

#### `src/guardrails/sql_ast_checker.py`
Implements an **inverted allowlist security model** using `sqlglot`:
- **`SAFE_NODE_TYPES`**: An immutable frozenset containing only permitted AST expression classes (`exp.Select`, `exp.From`, `exp.Where`, `exp.Group`, `exp.Having`, `exp.Order`, `exp.Limit`, `exp.Join`, `exp.Window`, `exp.DPipe`, `exp.Var`, `exp.Case`, etc.). Every node not explicitly present or subclassed is rejected with `ASTValidationError(violation_type="unsafe_node")`.
- **`BLOCKED_FUNCTIONS`**: Explicit blocklist preventing unauthorized table and utility functions (`read_csv`, `read_parquet`, `read_json`, `read_blob`, `glob`, `export_database`, `copy`, `attach`, `install`, `load`, `system`, `shell`, `getenv`, `duckdb_settings`, `char`, `chr`, `generate_series`, `repeat`).
- **Structural Attack Mitigations**:
  - **`_check_recursive_cte(tree)`**: Detects and rejects `WITH RECURSIVE` constructs to eliminate algorithmic complexity and infinite-loop DoS attacks.
  - **`_check_excessive_cross_joins(tree)`**: Detects $\ge 2$ unconstrained cross joins that produce Cartesian explosions.
  - **`_check_network_urls(tree)`**: Scans table names and string literals for remote URI schemes (`http://`, `https://`, `s3://`, `gcs://`).
  - **`_check_tautologies(tree)`**: Detects SQL injection tautologies such as `OR 1=1` or `'a'='a'`.
  - **`_check_string_escapes(tree)`**: Protects against parser-differential attacks where backslash-escaped quotes (`\'`) hide semicolons and DDL/DML statements (`DROP TABLE`) inside string literals.
- **`_ensure_limit(tree, max_rows)`**: Automatically injects a `LIMIT max_rows` clause if omitted, or clamps existing limits that exceed system thresholds.

#### `src/guardrails/input_sanitizer.py`
Pre-filter executing before queries reach LLM prompts:
- **Class `InputSanitizer`**:
  - **Length validation**: Rejects inputs $>2000$ characters.
  - **Regex pre-filters**: Identifies obvious SQL mutation tokens (`DROP TABLE`, `DELETE FROM`, `ALTER TABLE`, `TRUNCATE TABLE`, `EXEC sp_`).
  - **Prompt Injection Defense**: Detects jailbreak signatures (`ignore previous instructions`, `system prompt`, `you are now DAN`, `developer mode`).
  - **Encoding Obfuscation**: Flags excessive hex escapes (`0x...`), unicode escaping (`\u00...`), and null bytes.

---

### 4.4. Adaptive Model Routing (`src/router/router.py`)

Controls the cost and latency decision boundary:
- **Enum `ModelTier`**: `TIER_1_SLM` vs `TIER_2_FRONTIER`.
- **Class `QueryRouter`**:
  - **Heuristic Classifier**:
    - Calculates query complexity score based on keyword triggers:
      - Window functions (`over`, `rank`, `row_number`, `lag`, `lead`): $+3$
      - Analytical math (`correlation`, `percentile`, `cohort`, `retention`): $+3$
      - Joins required ($\ge 3$ tables): $+2$
      - Date arithmetic (`datediff`, `interval`, `month-over-month`): $+2$
      - Subqueries / CTEs (`with`, `subquery`, `nested`): $+2$
    - A score $\ge 3$ routes to **Tier 2 Frontier**.
    - A score $< 3$ routes to **Tier 1 SLM**.
  - **Semantic Ambiguity Detection**:
    - Compares user queries against known ambiguous phrasing patterns using cosine similarity.
    - Flags ambiguous queries and appends an interpretation note to guide downstream reasoning.

---

### 4.5. Schema Linking & Semantic Layer (`src/schema/`)

#### `src/schema/linker.py`
Solves the problem of prompt token bloating by dynamically injecting only the tables relevant to the user query:
- **Class `SchemaLinker`**:
  - Uses `sentence-transformers/all-MiniLM-L6-v2` to embed table names, descriptions, and column catalogues into 384-dimensional dense vectors.
  - Precomputes and caches schema embeddings on initialization.
  - Encodes the user question and computes cosine similarity against all tables.
  - Returns the top $K$ relevant tables (default: 4), ensuring LLM prompts remain concise and focused.

#### `src/schema/semantic_layer.py`
Enforces governed corporate metrics:
- **Class `SemanticLayer`**:
  - Maintains a repository of standardized enterprise metrics:
    - `revenue`: `SUM(order_items.price)`
    - `freight_revenue`: `SUM(order_items.freight_value)`
    - `total_orders`: `COUNT(DISTINCT orders.order_id)`
    - `average_order_value (AOV)`: `SUM(price) / COUNT(DISTINCT order_id)`
    - `delivery_delay_days`: `DATE_DIFF('day', order_estimated_delivery_date, order_delivered_customer_date)`
    - `review_score_avg`: `AVG(order_reviews.review_score)`
  - Matches user queries against metric definitions and aliases.
  - Automatically augments schema linker results with tables required to calculate referenced metrics.

---

### 4.6. Multi-Agent Orchestration & State Graph (`src/agents/`)

#### `src/agents/state.py`
Defines the centralized state dict propagated through the LangGraph state machine:
- `user_query`: Raw natural language input.
- `cleaned_query`: Sanitized input.
- `guardrail_passed`: Boolean security check status.
- `guardrail_rejection_reason`: Rejection explanation if blocked.
- `route_decision`: `TIER_1_SLM` or `TIER_2_FRONTIER`.
- `route_confidence`: Confidence score (0.0 to 1.0).
- `linked_tables`: Tables identified by schema linker.
- `linked_ddl`: Extracted DDL schema for linked tables.
- `metric_context`: Formula definitions from semantic layer.
- `generated_sql`: SQL string produced by model.
- `ast_valid`: Boolean AST allowlist validation status.
- `ast_error`: Error message from AST checker if invalid.
- `query_result`: Serialized `QueryResult` dict containing row count and parquet path.
- `execution_error`: Database runtime exception if query failed.
- `retry_count`: Current self-healing retry count.
- `max_retries`: Retry ceiling (default: 2).
- `error_history`: Accumulated chronological list of errors across retry attempts.
- `analysis_result`: Key findings, anomalies, and structured takeaways.
- `chart_specs`: Plotly chart specifications and figure data.
- `report_paths`: Generated PDF and Excel workbook filepaths.
- `final_response`: Complete formatted text response.
- `total_cost_usd`: Cumulative token cost across all LLM invocations.
- `total_latency_ms`: Total execution time.

#### `src/agents/graph.py`
Wired using LangGraph `StateGraph(AgentState)`:
1. **`guardrail`**: Executes `guardrail_node`. If input fails, routes to `terminal_reject` $\to$ `END`. If clean, routes to `route_query`.
2. **`route_query`**: Executes `router_node`. Links schema, injects metric context, determines model tier.
3. **`generate_sql`**: Executes `sql_generator_node`. Invokes the assigned LLM tier with strict instructions.
4. **`validate_ast`**: Executes `ast_validator_node`. Parses and verifies query through `sql_ast_checker`. If unsafe/invalid, routes to `heal`. If valid, routes to `execute_sql`.
5. **`execute_sql`**: Executes `execution_node` inside the sandboxed DuckDB connection. If database error occurs, routes to `heal`. If successful, routes to `analyze_data`.
6. **`heal`**: Executes `heal_node`. Increments `retry_count`, logs error context, and **escalates Tier 1 SLM to Tier 2 Frontier**. If `retry_count > max_retries`, routes to `terminal_error` $\to$ `END`. Otherwise, loops back to `generate_sql`.
7. **`analyze_data`**: Executes `analysis_node`. Checks for empty datasets, NULLs, anomalies, and synthesizes executive insights.
8. **`generate_visuals`**: Executes `visualizer_node`. Synthesizes Plotly figure specifications.
9. **`compile_reports`**: Executes `report_node`. Compiles PDF and Excel artifacts.
10. **`format_response`**: Formats final Markdown summary and model metrics $\to$ `END`.

---

### 4.7. Visualization & Multi-Modal Reports (`src/agents/nodes/`)

#### `src/agents/nodes/visualizer_node.py`
- Reads query results from the sandboxed Parquet file.
- Infers chart type deterministically if LLM suggestion is missing or invalid:
  - Temporal column + Numeric column $\to$ **Line Chart**.
  - Categorical column ($\le 15$ distinct) + Numeric column $\to$ **Bar Chart**.
  - 2 Numeric columns $\to$ **Scatter Plot**.
  - Categorical breakdown of a total $\le 7$ slices $\to$ **Pie / Donut Chart**.
- Constructs complete Plotly figure dictionaries compatible with Plotly.js and Streamlit.

#### `src/agents/nodes/report_node.py`
- **PDF Generation**:
  - Uses Jinja2 with `autoescape=True` to prevent template injection (XSS).
  - Employs `WeasyPrint` with a **hardened URL fetcher** that rejects all `file://`, `ftp://`, and remote `http://` requests, completely blocking Server-Side Request Forgery (SSRF) or local file read attacks.
- **Excel Generation**:
  - Uses `XlsxWriter` configured with `strings_to_formulas=False`.
  - Escapes leading characters (`=`, `+`, `-`, `@`, `\t`, `\r`) to completely mitigate **CSV/Excel Formula Injection (DDE attacks)**.

---

### 4.8. Frontend Implementations

#### Clean Enterprise Web UI (`web/`)
Designed specifically for corporate environments requiring a minimal, responsive dashboard with **strictly no emojis**:
- `web/index.html`: Clean Bootstrap 5.3 markup with responsive grid, query chips, metric ribbons, and result tabs.
- `web/style.css`: Enterprise color palette (`#0f2b48` primary, `#f4f6f9` background), crisp cards, syntax-highlighted SQL blocks, and custom status indicators.
- `web/app.js`: Vue.js 3 app handling real-time query dispatch, health check polling, tab switching, and Plotly chart rendering.

#### Streamlit Dashboard (`ui/app.py`)
- Full-featured analyst interface offering interactive sidebars, database schema viewer, cost tracker, and live query execution.

#### FastAPI Server (`src/api/server.py`)
- Serves the Vue.js frontend at `/`.
- Exposes `POST /api/query`, `GET /api/health`, and `GET /api/download`.
- Strips any unicode emoji characters from response payloads to guarantee an emoji-free user interface.

---

## 5. Security Architecture & Threat Model

The platform was subjected to extensive red-teaming against the **15 core attack categories** detailed in `src/security/red_team_payloads.json`.

| Threat Category | Example Red-Team Attack Vector | Mitigation Layer | Status |
| :--- | :--- | :--- | :---: |
| **Filesystem Read** | `SELECT * FROM read_csv('/etc/passwd')` | DuckDB `enable_external_access=false` + AST Blocked Functions | **BLOCKED** |
| **Multi-Statement** | `SELECT 1; DROP TABLE orders;` | AST Single Statement Enforcement (`len(stmts) == 1`) | **BLOCKED** |
| **Data Mutation** | `DELETE FROM orders WHERE 1=1` | DuckDB `read_only=True` + AST Root SELECT Check | **BLOCKED** |
| **Schema Mutation** | `ALTER TABLE orders ADD COLUMN pwned TEXT` | DuckDB `read_only=True` + AST Non-Select Root Block | **BLOCKED** |
| **Database Attach** | `ATTACH '/tmp/evil.db' AS exfil` | AST Non-Select Root Block + Blocked Function | **BLOCKED** |
| **Extension Loading** | `INSTALL httpfs; LOAD httpfs;` | DuckDB Sandbox + AST Command Node Rejection | **BLOCKED** |
| **Data Exfiltration** | `COPY orders TO '/tmp/exfil.csv'` | AST Copy Node Rejection + DuckDB External Access Off | **BLOCKED** |
| **Config Overrides** | `SET enable_external_access = true` | DuckDB `lock_configuration=true` + AST Command Block | **BLOCKED** |
| **System Info Disclosure**| `SELECT * FROM duckdb_settings()` | AST Blocked Functions (`duckdb_*`, `current_setting`) | **BLOCKED** |
| **Recursion DoS** | `WITH RECURSIVE bomb AS (...)` | AST `_check_recursive_cte` Traversal Block | **BLOCKED** |
| **Cartesian Explosion**| `orders o1 CROSS JOIN orders o2 CROSS JOIN ...` | AST `_check_excessive_cross_joins` Limit Check | **BLOCKED** |
| **String Escape Differential**| `SELECT '\''; DROP TABLE orders; --'` | AST `_check_string_escapes` Regex Scan | **BLOCKED** |
| **SQL Tautologies** | `WHERE customer_id = '' OR 1=1` | AST `_check_tautologies` Equality Evaluator | **BLOCKED** |
| **Network Access** | `SELECT * FROM 'https://evil.com/data.csv'`| AST `_check_network_urls` Table Reference Scanner | **BLOCKED** |
| **Encoding Bypasses** | `WHERE id = CHAR(68)\|\|CHAR(82)...` | AST Blocked Functions (`char`, `chr`) | **BLOCKED** |

---

## 6. Testing & Quality Assurance

The codebase maintains **118 automated tests** with 100% pass rate:
- **Unit Verification (`test_ast_checker.py`)**: 39 tests verifying parser edge cases, nested queries, subqueries, and allowlist rules.
- **Red-Team Suite (`test_red_team.py`)**: 60 tests ensuring 50/50 attack payloads from `red_team_payloads.json` are rejected with 0 bypasses.
- **Execution & Sandbox Tests (`test_sandbox_execution.py`)**: 11 tests verifying DuckDB configuration locks, timeouts, row limits, and Parquet caching.
- **Agent Integration (`test_agent_graph.py`)**: 8 tests verifying LangGraph DAG compilation, guardrail rejection exits, self-healing tier escalation, and schema linking.
