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
    import sqlglot

    fences = re.findall(r"```(?:sql)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    cleaned = fences[0].strip() if fences else raw.strip()

    match = re.search(r"((?:WITH\s+(?:RECURSIVE\s+)?[a-zA-Z_][a-zA-Z0-9_]*\s+AS\s*\(|SELECT\b)[\s\S]+)", cleaned, re.IGNORECASE)
    if match:
        cleaned = match.group(1).strip()

    if ";" in cleaned:
        cleaned = cleaned.split(";")[0].strip()

    try:
        sqlglot.parse_one(cleaned, read="duckdb")
        return cleaned.rstrip(";")
    except Exception:
        pass

    limit_match = re.search(r"(.*?\bLIMIT\s+\d+)", cleaned, re.IGNORECASE | re.DOTALL)
    if limit_match:
        candidate = limit_match.group(1).strip()
        try:
            sqlglot.parse_one(candidate, read="duckdb")
            return candidate
        except Exception:
            pass

    lines = cleaned.split("\n")
    while len(lines) > 1:
        lines.pop()
        candidate = "\n".join(lines).strip().rstrip(";")
        try:
            sqlglot.parse_one(candidate, read="duckdb")
            return candidate
        except Exception:
            continue

    words = cleaned.split()
    while len(words) > 4:
        words.pop()
        candidate = " ".join(words).strip().rstrip(";")
        try:
            sqlglot.parse_one(candidate, read="duckdb")
            return candidate
        except Exception:
            continue

    return cleaned.rstrip(";")


def sql_generator_node(state: AgentState) -> dict[str, Any]:
    query = state["cleaned_query"]
    tier = state["route_decision"]
    ddl = state["linked_ddl"]
    metric_ctx = state.get("metric_context", "")
    retry_count = state.get("retry_count", 0)
    error_history = state.get("error_history", [])

    client = _get_client()
    model_tier = "tier1" if tier == "TIER_1_SLM" else "tier2"

    if retry_count > 0 and state.get("generated_sql") and state.get("execution_error"):
        system, user = build_self_heal_prompt(
            user_query=query,
            schema_ddl=ddl,
            failed_sql=state["generated_sql"],
            error_message=state.get("execution_error", ""),
            error_history=error_history,
            metric_context=metric_ctx,
        )
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
