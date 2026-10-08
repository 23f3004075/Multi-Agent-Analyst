from __future__ import annotations

import logging
from typing import Any

from src.agents.state import AgentState
from src.guardrails.sql_ast_checker import ASTValidationError, validate_sql

logger = logging.getLogger(__name__)


def ast_validator_node(state: AgentState) -> dict[str, Any]:
    sql = state.get("generated_sql")

    if not sql or not sql.strip():
        return {
            "ast_valid": False,
            "ast_error": "No executable SQL statement found in model response (model outputted conversational text or questions instead of a SELECT query).",
        }

    logger.info("AST validation: %s", sql[:200])

    try:
        cleaned_sql = validate_sql(sql)
        logger.info("AST validation passed")
        return {
            "ast_valid": True,
            "ast_error": None,
            "generated_sql": cleaned_sql,
        }
    except ASTValidationError as e:
        logger.warning("AST validation failed: %s", e)
        return {
            "ast_valid": False,
            "ast_error": str(e),
        }
