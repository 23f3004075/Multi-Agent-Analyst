"""
SQL Generator Node — Generates SQL via the routed model tier.

Calls the LLM with schema DDL, metric context, and the user query.
For self-healing retries, includes error context and prior attempts.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.agents.state import AgentState
from src.config import get_settings
from src.llm.client import LLMClient
from src.llm.prompts.sql_generation import (
    build_self_heal_prompt,
    build_sql_generation_prompt,
)

logger = logging.getLogger(__name__)

_llm_client: LLMClient | None = None


def _get_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(get_settings())
    return _llm_client


def _extract_sql(raw: str) -> str:
    """
    Extract SQL from LLM response, stripping markdown fences and explanation.

    Handles common patterns:
        - ```sql\nSELECT...\n```
        - ```\nSELECT...\n```
        - Trailing explanations after semicolon
        - Just the raw SQL
    """
    # 1. Extract from markdown code fences if present
    fences = re.findall(r"```(?:sql)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    cleaned = fences[0].strip() if fences else raw.strip()

    # 2. Extract starting from first WITH or SELECT
    match = re.search(r"((?:WITH|SELECT)\b[\s\S]+)", cleaned, re.IGNORECASE)
    if match:
        cleaned = match.group(1).strip()

    # 3. If there is a trailing explanation after a terminating semicolon, trim it
    parts = re.split(r";(?:\s*\n|\s*$)", cleaned)
    if parts and parts[0].strip():
        cleaned = parts[0].strip()

    return cleaned.rstrip(";")


def sql_generator_node(state: AgentState) -> dict[str, Any]:
    """
    Generate SQL using the routed model tier.

    Produces: generated_sql, model_used, total_cost_usd
    """
    query = state["cleaned_query"]
    tier = state["route_decision"]
    ddl = state["linked_ddl"]
    metric_ctx = state.get("metric_context", "")
    retry_count = state.get("retry_count", 0)
    error_history = state.get("error_history", [])

    client = _get_client()
    model_tier = "tier1" if tier == "TIER_1_SLM" else "tier2"

    # Choose prompt based on whether this is a first attempt or self-heal
    if retry_count > 0 and state.get("generated_sql") and state.get("execution_error"):
        # Self-healing: include error context
        system, user = build_self_heal_prompt(
            user_query=query,
            schema_ddl=ddl,
            failed_sql=state["generated_sql"],
            error_message=state.get("execution_error", ""),
            error_history=error_history,
            metric_context=metric_ctx,
        )
        # Always use Tier 2 for self-healing
        model_tier = "tier2"
    else:
        system, user = build_sql_generation_prompt(
            user_query=query,
            schema_ddl=ddl,
            metric_context=metric_ctx,
            tier=model_tier,
        )

    logger.info(
        "Generating SQL: tier=%s, retry=%d, query='%s'",
        model_tier, retry_count, query[:60],
    )

    response = client.generate(
        prompt=user,
        system=system,
        model_tier=model_tier,
        temperature=0.0,
        max_tokens=1024,
        reasoning=(retry_count > 0),
    )

    sql = _extract_sql(response.content)
    logger.info("Generated SQL (%s): %s", response.model_used, sql[:200])

    prev_cost = state.get("total_cost_usd", 0.0)

    return {
        "generated_sql": sql,
        "model_used": response.model_used,
        "total_cost_usd": prev_cost + response.cost_usd,
    }
