from __future__ import annotations


SQL_SYSTEM_PROMPT = """\
You are a precise SQL query generator for a DuckDB analytical database.

RULES:
1. Generate ONLY a single SELECT statement (or WITH/CTE wrapping a SELECT).
2. Use DuckDB SQL dialect.
3. NEVER use DROP, DELETE, UPDATE, INSERT, ALTER, CREATE, ATTACH, COPY, or PRAGMA.
4. NEVER use read_csv, read_parquet, glob, or any file-reading functions.
5. Always include a LIMIT clause (max 5000 rows) unless the query is an aggregation.
6. Use proper table aliases and explicit column references.
7. For date operations, use DuckDB functions: DATE_TRUNC, DATE_DIFF, EXTRACT, etc.
8. If a question is ambiguous, use the most reasonable interpretation. Any assumption must be inside a SQL comment (-- comment), NEVER as free text.
9. Output ONLY the raw SQL query. Do NOT add notes, explanations, or text outside the SQL query.
"""

SQL_SYSTEM_PROMPT_TIER2 = """\
You are an expert SQL analyst generating precise DuckDB SQL for complex analytical queries.

RULES:
1. Generate ONLY a single SELECT statement (or WITH/CTE wrapping a SELECT).
2. Use DuckDB SQL dialect (supports CTEs, window functions, UNNEST, QUALIFY, etc.).
3. NEVER use DROP, DELETE, UPDATE, INSERT, ALTER, CREATE, ATTACH, COPY, or PRAGMA.
4. NEVER use read_csv, read_parquet, glob, or any file-reading functions.
5. Always include a LIMIT clause (max 5000 rows) unless the query is an aggregation.
6. Use proper table aliases and explicit column references.
7. For multi-step analysis, use CTEs for clarity and readability.
8. For time-series: use DATE_TRUNC for grouping, EXTRACT for components.
9. For rankings: use ROW_NUMBER(), RANK(), or DENSE_RANK() with proper PARTITION BY.
10. If a question is ambiguous, use the most reasonable interpretation. Any assumption must be inside a SQL comment (-- comment), NEVER as free text.
11. Output ONLY the raw SQL query. Do NOT add notes, explanations, or text outside the SQL query.
"""


SQL_USER_PROMPT_TEMPLATE = """\
## Database Schema
{schema_ddl}

{metric_context}

## DuckDB Dialect Examples
- Date truncation: DATE_TRUNC('month', order_purchase_timestamp)
- Date difference: DATE_DIFF('day', start_date, end_date)
- String matching: column ILIKE '%pattern%'
- Null handling: COALESCE(column, default_value)
- Conditional agg: COUNT(*) FILTER (WHERE condition)

## Question
{user_query}

Generate the SQL query:"""


SQL_SELF_HEAL_PROMPT_TEMPLATE = """\
## Database Schema
{schema_ddl}

{metric_context}

## Previous Attempt
The following SQL query failed:
```sql
{failed_sql}
```

## Error
{error_message}

## Error History
{error_history}

## Instructions
Fix the SQL query to address the error above. Common fixes:
- Table not found: ONLY select from tables explicitly defined in Database Schema above. NEVER invent table names.
- Column not found: check the schema for correct column names
- Syntax error: ensure DuckDB dialect compatibility
- Type mismatch: add explicit CAST() where needed
- Ambiguous column: use table aliases (e.g., o.order_id, not order_id)

Generate ONLY the corrected SQL query:"""


ANALYSIS_SYSTEM_PROMPT = """\
You are a data analyst producing concise, actionable insights from SQL query results.

RULES:
1. Summarize the key findings in 2-4 bullet points.
2. Highlight notable patterns, anomalies, or trends.
3. Use specific numbers from the data — never invent or estimate values.
4. If results were truncated, acknowledge the limitation.
5. Suggest a chart type that would best visualize this data.
6. Respond in JSON format with the following structure:
{
  "summary": "2-3 sentence executive summary",
  "key_findings": ["finding 1", "finding 2", ...],
  "anomalies": ["anomaly 1", ...] or [],
  "suggested_chart": {
    "chart_type": "bar|line|scatter|pie|kpi_card",
    "x_axis": "column_name or null",
    "y_axis": "column_name or null",
    "group_by": "column_name or null",
    "title": "Chart Title"
  },
  "interpretation_note": "any assumptions made" or null
}
"""

ANALYSIS_USER_PROMPT_TEMPLATE = """\
## Original Question
{user_query}

## SQL Query Executed
```sql
{sql_query}
```

## Query Results
{result_context}

Analyze these results and respond in the JSON format specified:"""


def build_sql_generation_prompt(
    user_query: str,
    schema_ddl: str,
    metric_context: str = "",
    tier: str = "tier1",
) -> tuple[str, str]:
    system = SQL_SYSTEM_PROMPT if tier == "tier1" else SQL_SYSTEM_PROMPT_TIER2

    user = SQL_USER_PROMPT_TEMPLATE.format(
        schema_ddl=schema_ddl,
        metric_context=metric_context,
        user_query=user_query,
    )

    return system, user


def build_self_heal_prompt(
    user_query: str,
    schema_ddl: str,
    failed_sql: str,
    error_message: str,
    error_history: list[str],
    metric_context: str = "",
) -> tuple[str, str]:
    history_text = "\n".join(
        f"  Attempt {i + 1}: {err}" for i, err in enumerate(error_history)
    ) if error_history else "  No previous errors."

    user = SQL_SELF_HEAL_PROMPT_TEMPLATE.format(
        schema_ddl=schema_ddl,
        metric_context=metric_context,
        failed_sql=failed_sql,
        error_message=error_message,
        error_history=history_text,
    )

    return SQL_SYSTEM_PROMPT_TIER2, user


def build_analysis_prompt(
    user_query: str,
    sql_query: str,
    result_context: str,
) -> tuple[str, str]:
    user = ANALYSIS_USER_PROMPT_TEMPLATE.format(
        user_query=user_query,
        sql_query=sql_query,
        result_context=result_context,
    )

    return ANALYSIS_SYSTEM_PROMPT, user
