from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from src.agents.state import AgentState
from src.config import get_settings
from src.database.connection import create_secure_connection
from src.database.executor import SandboxedExecutor

logger = logging.getLogger(__name__)

_executor: SandboxedExecutor | None = None


def _get_executor() -> SandboxedExecutor:
    global _executor
    if _executor is None:
        _executor = SandboxedExecutor(get_settings())
    return _executor


def execution_node(state: AgentState) -> dict[str, Any]:
    sql = state.get("generated_sql")

    if not sql:
        return {
            "query_result": None,
            "execution_error": "No SQL to execute",
        }

    logger.info("Executing SQL: %s", sql[:200])
    executor = _get_executor()
    settings = get_settings()

    try:
        conn = create_secure_connection(settings, db_path=state.get("db_path"))
        try:
            result = executor.execute(sql, conn=conn)

            result_dict = {
                "result_id": result.result_id,
                "parquet_path": result.parquet_path,
                "schema": result.schema,
                "row_count": result.row_count,
                "column_count": result.column_count,
                "truncated": result.truncated,
                "sample_rows": result.sample_rows,
                "summary_stats": result.summary_stats,
                "llm_context": result.to_llm_context(),
            }

            logger.info(
                "Execution success: %d rows × %d cols (truncated=%s)",
                result.row_count,
                result.column_count,
                result.truncated,
            )

            return {
                "query_result": result_dict,
                "execution_error": None,
            }

        finally:
            conn.close()

    except TimeoutError as e:
        logger.warning("Query timeout: %s", e)
        return {
            "query_result": None,
            "execution_error": f"Query timeout: {e}",
        }
    except Exception as e:
        logger.warning("Execution error: %s", e)
        return {
            "query_result": None,
            "execution_error": f"Runtime error: {type(e).__name__}: {e}",
        }
