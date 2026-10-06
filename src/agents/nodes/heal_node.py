from __future__ import annotations

import logging
from typing import Any

from src.agents.state import AgentState

logger = logging.getLogger(__name__)


def heal_node(state: AgentState) -> dict[str, Any]:
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)
    error_history = list(state.get("error_history", []))
    current_tier = state.get("route_decision", "TIER_1_SLM")

    error = (
        state.get("ast_error")
        or state.get("execution_error")
        or state.get("sanity_check_warning")
        or "Unknown error"
    )

    error_history.append(f"Attempt {retry_count + 1}: {error}")
    retry_count += 1

    logger.info(
        "Healing: attempt %d/%d, error='%s', current_tier=%s",
        retry_count, max_retries, error[:100], current_tier,
    )

    if current_tier == "TIER_1_SLM":
        new_tier = "TIER_2_FRONTIER"
        logger.info("Escalating from Tier 1 to Tier 2")
    else:
        new_tier = "TIER_2_FRONTIER"
        logger.info("Retrying Tier 2 with error context")

    return {
        "retry_count": retry_count,
        "error_history": error_history,
        "route_decision": new_tier,
        "ast_valid": False,
        "ast_error": None,
    }
