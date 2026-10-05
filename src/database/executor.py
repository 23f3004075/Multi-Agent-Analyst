"""
Sandboxed SQL Query Executor.

Executes validated SQL against a hardened DuckDB connection with:
    - conn.interrupt() watchdog for real timeout enforcement
    - QueryResult reference type (Parquet file + metadata, NOT raw data in state)
    - Automatic truncation detection (fetches N+1 rows)
    - Summary statistics computation for LLM context

The executor does NOT validate SQL — that's the AST checker's job.
It assumes the SQL has already passed the allowlist validator.

Usage:
    from src.database.executor import SandboxedExecutor, QueryResult

    executor = SandboxedExecutor(settings)
    result: QueryResult = executor.execute("SELECT * FROM orders LIMIT 100")
    print(result.row_count, result.schema, result.sample_rows)
"""

from __future__ import annotations

import logging
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import duckdb
import pandas as pd

from src.config import Settings
from src.database.connection import create_secure_connection

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# QueryResult: lightweight reference type for LangGraph state
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueryResult:
    """
    Lightweight query result reference.

    Stores a path to the full result (Parquet) plus metadata summary.
    This keeps the LangGraph state checkpoint small (~2 KB) instead of
    serializing entire DataFrames (~10-50 MB for 5000 rows).

    The Parquet file can be read by downstream nodes (visualizer, report
    builder) without passing through the state serialization layer.
    """

    # Identity
    result_id: str

    # Data reference — downstream nodes read this file directly
    parquet_path: str

    # Schema — column names and types (for LLM prompt context)
    schema: list[dict[str, str]]  # [{"name": "col", "type": "VARCHAR"}, ...]

    # Metadata
    row_count: int
    column_count: int
    truncated: bool  # True if LIMIT was hit (fetched max_rows+1)

    # LLM context — small enough to embed in prompts
    sample_rows: list[dict[str, Any]]  # First 5 rows as dicts
    summary_stats: dict[str, Any]  # Computed aggregates per numeric column

    def to_llm_context(self) -> str:
        """
        Format result metadata for LLM prompt injection.

        Returns a compact string with schema, sample, and stats —
        everything the LLM needs for analysis and chart generation
        without seeing all 5000 rows.
        """
        lines = [
            f"Query returned {self.row_count} rows × {self.column_count} columns.",
        ]
        if self.truncated:
            lines.append(
                "⚠️ Results were TRUNCATED — the full dataset may be larger."
            )

        # Schema
        lines.append("\nColumns:")
        for col in self.schema:
            lines.append(f"  - {col['name']} ({col['type']})")

        # Sample rows
        lines.append(f"\nSample (first {len(self.sample_rows)} rows):")
        for i, row in enumerate(self.sample_rows):
            lines.append(f"  Row {i + 1}: {row}")

        # Summary stats
        if self.summary_stats:
            lines.append("\nSummary Statistics:")
            for col_name, stats in self.summary_stats.items():
                lines.append(f"  {col_name}: {stats}")

        return "\n".join(lines)


@dataclass
class ExecutionError:
    """Structured error from a failed query execution."""

    error_type: str  # "timeout", "runtime", "memory"
    message: str
    sql: str
    suggestion: str = ""


# ─────────────────────────────────────────────────────────────────────
# Sandboxed Executor
# ─────────────────────────────────────────────────────────────────────


class SandboxedExecutor:
    """
    Executes SQL in a security-hardened DuckDB sandbox.

    Key design decisions:
    1. Uses conn.interrupt() for REAL timeout enforcement (not just
       thread abandonment like concurrent.futures).
    2. Returns QueryResult references, not raw DataFrames.
    3. Detects truncation by fetching max_rows+1.
    4. Computes summary stats for LLM context.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._temp_dir = Path(tempfile.mkdtemp(prefix="sql_agent_results_"))
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Executor temp dir: %s", self._temp_dir)

    def execute(
        self,
        sql: str,
        conn: duckdb.DuckDBPyConnection | None = None,
        timeout_seconds: float | None = None,
    ) -> QueryResult:
        """
        Execute SQL with watchdog-based timeout and return a QueryResult.

        Args:
            sql: The SQL query to execute (must have passed AST validation).
            conn: Optional pre-existing connection. If None, creates a new one.
            timeout_seconds: Override for query timeout. Defaults to settings.

        Returns:
            QueryResult with Parquet reference and metadata.

        Raises:
            TimeoutError: Query exceeded the timeout.
            ExecutionError: Query failed at runtime.
        """
        timeout = timeout_seconds or self._settings.db_query_timeout_seconds
        owns_conn = conn is None

        if owns_conn:
            conn = create_secure_connection(self._settings)

        try:
            df = self._execute_with_watchdog(conn, sql, timeout)
            return self._build_result(df, sql)
        finally:
            if owns_conn:
                conn.close()

    def _execute_with_watchdog(
        self,
        conn: duckdb.DuckDBPyConnection,
        sql: str,
        timeout: float,
    ) -> pd.DataFrame:
        """
        Execute SQL with a watchdog thread that calls conn.interrupt()
        on timeout — providing REAL query cancellation.

        Unlike concurrent.futures timeout (which just abandons the Python
        thread while the C++ query engine continues consuming resources),
        conn.interrupt() sends a cancellation signal to DuckDB's execution
        engine, which terminates the query and frees resources.
        """
        result_container: dict[str, pd.DataFrame] = {}
        error_container: dict[str, Exception] = {}
        execution_done = threading.Event()

        def _run_query() -> None:
            try:
                result_container["df"] = conn.execute(sql).fetchdf()
            except Exception as e:
                error_container["error"] = e
            finally:
                execution_done.set()

        def _watchdog() -> None:
            """Wait for timeout, then interrupt if query is still running."""
            if not execution_done.wait(timeout=timeout):
                logger.warning(
                    "Query timeout (%.1fs) — sending conn.interrupt()", timeout
                )
                try:
                    conn.interrupt()
                except Exception:
                    logger.error("Failed to interrupt DuckDB connection", exc_info=True)

        # Start query thread and watchdog
        query_thread = threading.Thread(target=_run_query, name="duckdb-query")
        watchdog_thread = threading.Thread(target=_watchdog, name="duckdb-watchdog")

        query_thread.start()
        watchdog_thread.start()

        # Wait for query to complete (watchdog will interrupt if needed)
        query_thread.join(timeout=timeout + 2.0)  # Grace period after interrupt
        watchdog_thread.join(timeout=1.0)

        # Check results
        if query_thread.is_alive():
            raise TimeoutError(
                f"Query did not terminate within {timeout + 2.0}s even after "
                f"conn.interrupt(). This may indicate a DuckDB bug. SQL: {sql[:200]}"
            )

        if "error" in error_container:
            err = error_container["error"]
            # DuckDB raises InterruptException on conn.interrupt()
            if "interrupt" in str(err).lower():
                raise TimeoutError(
                    f"Query cancelled after {timeout}s timeout. SQL: {sql[:200]}"
                )
            raise err

        return result_container["df"]

    def _build_result(self, df: pd.DataFrame, sql: str) -> QueryResult:
        """
        Convert a pandas DataFrame into a QueryResult reference.

        1. Detect truncation (if row_count == max_result_rows)
        2. Save full DataFrame to Parquet
        3. Extract schema, sample, and summary stats
        """
        max_rows = self._settings.db_max_result_rows
        truncated = len(df) >= max_rows

        if truncated:
            # We fetched N+1 rows — trim to N and flag truncation
            df = df.head(max_rows - 1)
            logger.info(
                "Result truncated: fetched %d rows (limit %d)",
                len(df),
                max_rows - 1,
            )

        # Generate unique result ID and save to Parquet
        result_id = str(uuid.uuid4())[:8]
        parquet_path = self._temp_dir / f"result_{result_id}.parquet"
        df.to_parquet(str(parquet_path), engine="pyarrow", index=False)

        # Extract schema
        schema = [
            {"name": str(col), "type": str(dtype)}
            for col, dtype in zip(df.columns, df.dtypes)
        ]

        # Sample rows (first 5)
        sample_df = df.head(5)
        sample_rows = sample_df.to_dict(orient="records")

        # Summary statistics for numeric columns
        summary_stats = self._compute_summary_stats(df)

        return QueryResult(
            result_id=result_id,
            parquet_path=str(parquet_path),
            schema=schema,
            row_count=len(df),
            column_count=len(df.columns),
            truncated=truncated,
            sample_rows=sample_rows,
            summary_stats=summary_stats,
        )

    @staticmethod
    def _compute_summary_stats(df: pd.DataFrame) -> dict[str, Any]:
        """
        Compute summary statistics for numeric columns.

        These stats are passed to the LLM for narration and anomaly
        detection — the LLM never sees the full dataset.
        """
        stats: dict[str, Any] = {}

        for col in df.select_dtypes(include=["number"]).columns:
            col_stats: dict[str, Any] = {}
            series = df[col].dropna()

            if len(series) == 0:
                col_stats["all_null"] = True
            else:
                col_stats["min"] = float(series.min())
                col_stats["max"] = float(series.max())
                col_stats["mean"] = round(float(series.mean()), 2)
                col_stats["median"] = round(float(series.median()), 2)
                col_stats["std"] = round(float(series.std()), 2) if len(series) > 1 else 0.0
                col_stats["null_count"] = int(df[col].isna().sum())
                col_stats["distinct_count"] = int(series.nunique())

            stats[str(col)] = col_stats

        # Add categorical column cardinalities
        for col in df.select_dtypes(include=["object", "category", "string", "str"]).columns:
            n_unique = df[col].nunique()
            null_count = int(df[col].isna().sum())
            stats[str(col)] = {
                "type": "categorical",
                "distinct_count": n_unique,
                "null_count": null_count,
                "top_values": df[col].value_counts().head(5).to_dict(),
            }

        return stats

    def cleanup(self) -> None:
        """Remove all temporary Parquet files."""
        import shutil

        if self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            logger.info("Cleaned up temp dir: %s", self._temp_dir)
