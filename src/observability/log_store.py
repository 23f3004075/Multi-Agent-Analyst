"""
Enterprise SQL Agent — Observability & Telemetry Log Store.

Persists comprehensive metrics for every query execution:
    - Model Tier (Tier 1: SLM vs Tier 2: Frontier)
    - Router Confidence (%)
    - Total Latency (ms / s)
    - Total Cost ($)
    - Retry Attempts
    - Security Check (Passed vs Blocked)
    - Execution Status (SUCCESS, FAILED, BLOCKED)
    - Error History / Failure Details
    - Generated SQL & Rows Returned

Storage: Embedded thread-safe SQLite database at data/query_telemetry.db
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Base path for database
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "query_telemetry.db"

_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    """Return a configured SQLite connection with WAL mode enabled."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_log_store() -> None:
    """Initialize telemetry tables and schema indices."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_tier TEXT NOT NULL,
                    model_used TEXT NOT NULL,
                    router_confidence REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    cost_usd REAL NOT NULL,
                    retry_count INTEGER NOT NULL,
                    security_check TEXT NOT NULL,
                    row_count INTEGER DEFAULT 0,
                    generated_sql TEXT DEFAULT '',
                    error_message TEXT DEFAULT ''
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON query_logs(timestamp DESC);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_status ON query_logs(status);"
            )
            conn.commit()
            logger.info("Telemetry log store initialized at %s", DB_PATH)
        except Exception as e:
            logger.error("Failed initializing telemetry log store: %s", e)
        finally:
            conn.close()


def record_query_log(
    *,
    query: str,
    status: str,
    model_tier: str,
    model_used: str = "N/A",
    router_confidence: float = 1.0,
    latency_ms: float = 0.0,
    cost_usd: float = 0.0,
    retry_count: int = 0,
    security_check: str = "Passed",
    row_count: int = 0,
    generated_sql: str = "",
    error_message: str = "",
) -> int:
    """
    Record an execution event in the telemetry store.
    
    Status should be one of: 'SUCCESS', 'FAILED', 'BLOCKED'.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_query = query.strip()
    clean_sql = (generated_sql or "").strip()
    clean_error = (error_message or "").strip()

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO query_logs (
                    timestamp, query, status, model_tier, model_used,
                    router_confidence, latency_ms, cost_usd, retry_count,
                    security_check, row_count, generated_sql, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    now_iso,
                    clean_query,
                    status.upper(),
                    model_tier,
                    model_used,
                    float(router_confidence),
                    float(latency_ms),
                    float(cost_usd),
                    int(retry_count),
                    security_check,
                    int(row_count),
                    clean_sql,
                    clean_error,
                ),
            )
            conn.commit()
            log_id = cursor.lastrowid or 0
            logger.info(
                "Logged query telemetry [ID=%d, Status=%s, Tier=%s, Latency=%.1fms, Cost=$%.4f]",
                log_id,
                status.upper(),
                model_tier,
                latency_ms,
                cost_usd,
            )
            return log_id
        except Exception as e:
            logger.error("Failed recording query telemetry: %s", e)
            return 0
        finally:
            conn.close()


def get_logs(
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Retrieve execution logs with optional status filtering."""
    with _lock:
        conn = _get_connection()
        try:
            if status and status.upper() in ("SUCCESS", "FAILED", "BLOCKED"):
                rows = conn.execute(
                    """
                    SELECT * FROM query_logs
                    WHERE status = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?;
                    """,
                    (status.upper(), limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM query_logs
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?;
                    """,
                    (limit, offset),
                ).fetchall()

            return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Failed querying telemetry logs: %s", e)
            return []
        finally:
            conn.close()


def get_stats() -> dict[str, Any]:
    """Calculate aggregate telemetry metrics and failure stats."""
    with _lock:
        conn = _get_connection()
        try:
            total_queries = conn.execute("SELECT COUNT(*) FROM query_logs;").fetchone()[0]
            if total_queries == 0:
                return {
                    "total_queries": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "blocked_count": 0,
                    "success_rate_pct": 100.0,
                    "avg_latency_ms": 0.0,
                    "total_cost_usd": 0.0,
                    "tier1_count": 0,
                    "tier2_count": 0,
                    "last_run_status": "IDLE",
                }

            success_cnt = conn.execute(
                "SELECT COUNT(*) FROM query_logs WHERE status = 'SUCCESS';"
            ).fetchone()[0]
            failed_cnt = conn.execute(
                "SELECT COUNT(*) FROM query_logs WHERE status = 'FAILED';"
            ).fetchone()[0]
            blocked_cnt = conn.execute(
                "SELECT COUNT(*) FROM query_logs WHERE status = 'BLOCKED';"
            ).fetchone()[0]

            avg_latency = conn.execute(
                "SELECT COALESCE(AVG(latency_ms), 0.0) FROM query_logs;"
            ).fetchone()[0]
            total_cost = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM query_logs;"
            ).fetchone()[0]

            tier1_cnt = conn.execute(
                "SELECT COUNT(*) FROM query_logs WHERE model_tier LIKE '%SLM%';"
            ).fetchone()[0]
            tier2_cnt = conn.execute(
                "SELECT COUNT(*) FROM query_logs WHERE model_tier LIKE '%Frontier%';"
            ).fetchone()[0]

            last_run = conn.execute(
                "SELECT status FROM query_logs ORDER BY id DESC LIMIT 1;"
            ).fetchone()
            last_run_status = last_run[0] if last_run else "IDLE"

            success_rate = round((success_cnt / total_queries) * 100, 1)

            return {
                "total_queries": total_queries,
                "success_count": success_cnt,
                "failed_count": failed_cnt,
                "blocked_count": blocked_cnt,
                "success_rate_pct": success_rate,
                "avg_latency_ms": round(avg_latency, 1),
                "total_cost_usd": round(total_cost, 4),
                "tier1_count": tier1_cnt,
                "tier2_count": tier2_cnt,
                "last_run_status": last_run_status,
            }
        except Exception as e:
            logger.error("Failed calculating telemetry stats: %s", e)
            return {
                "total_queries": 0,
                "success_count": 0,
                "failed_count": 0,
                "blocked_count": 0,
                "success_rate_pct": 0.0,
                "avg_latency_ms": 0.0,
                "total_cost_usd": 0.0,
                "tier1_count": 0,
                "tier2_count": 0,
                "last_run_status": "ERROR",
            }
        finally:
            conn.close()


def clear_logs() -> bool:
    """Clear all records from the telemetry log store."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("DELETE FROM query_logs;")
            conn.commit()
            return True
        except Exception as e:
            logger.error("Failed clearing telemetry logs: %s", e)
            return False
        finally:
            conn.close()


def export_logs_csv() -> str:
    """Export all telemetry logs as a CSV formatted string."""
    with _lock:
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM query_logs ORDER BY id DESC;"
            ).fetchall()
            output = io.StringIO()
            if rows:
                fieldnames = list(rows[0].keys())
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                for r in rows:
                    writer.writerow(dict(r))
            return output.getvalue()
        except Exception as e:
            logger.error("Failed exporting telemetry CSV: %s", e)
            return ""
        finally:
            conn.close()
