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
    from sqlglot import exp

    if not raw or not raw.strip():
        return ""

    # 1. Strip reasoning / thinking tags
    text = re.sub(r"<(?:think|thought)>[\s\S]*?</(?:think|thought)>", "", raw, flags=re.IGNORECASE).strip()

    # 2. Check markdown code fences in reverse order (models usually put explanation first, final SQL in last block)
    fences = re.findall(r"```(?:sql)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    for fence in reversed(fences):
        clean_fence = fence.strip().rstrip(";")
        try:
            parsed = sqlglot.parse_one(clean_fence, read="duckdb")
            if isinstance(parsed, (exp.Select, exp.Union)) or parsed.find(exp.Select):
                return clean_fence
        except Exception:
            continue

    # 3. Find candidate query starts (WITH or SELECT ... FROM) in reverse order
    query_start_pattern = re.compile(
        r"\b(WITH\s+[a-zA-Z_][a-zA-Z0-9_]*\s+AS\s*\(|SELECT\s+[\s\S]*?\bFROM\b)",
        re.IGNORECASE,
    )
    matches = list(query_start_pattern.finditer(text))

    for m in reversed(matches):
        candidate = text[m.start():].strip()
        if ";" in candidate:
            candidate = candidate.split(";")[0].strip()

        try:
            parsed = sqlglot.parse_one(candidate, read="duckdb")
            if isinstance(parsed, (exp.Select, exp.Union)) or parsed.find(exp.Select):
                return candidate.rstrip(";")
        except Exception:
            pass

        # Try trimming trailing explanatory lines from candidate
        cand_lines = candidate.split("\n")
        while len(cand_lines) > 1:
            cand_lines.pop()
            trimmed = "\n".join(cand_lines).strip().rstrip(";")
            try:
                parsed = sqlglot.parse_one(trimmed, read="duckdb")
                if isinstance(parsed, (exp.Select, exp.Union)) or parsed.find(exp.Select):
                    return trimmed
            except Exception:
                continue

    # 4. Fallback cleanup - only return if it actually parses as valid SQL (Select or Union)
    cleaned = text
    if fences:
        cleaned = fences[-1].strip()
    if ";" in cleaned:
        cleaned = cleaned.split(";")[0].strip()

    cleaned = cleaned.rstrip(";")
    try:
        parsed = sqlglot.parse_one(cleaned, read="duckdb")
        if isinstance(parsed, (exp.Select, exp.Union)) or parsed.find(exp.Select):
            return cleaned
    except Exception:
        pass

    return ""


def sql_generator_node(state: AgentState) -> dict[str, Any]:
    query = state["cleaned_query"]
    tier = state["route_decision"]
    ddl = state["linked_ddl"]
    metric_ctx = state.get("metric_context", "")
    retry_count = state.get("retry_count", 0)
    error_history = state.get("error_history", [])

    client = _get_client()
    model_tier = "tier1" if tier == "TIER_1_SLM" else "tier2"

    last_error = (
        state.get("execution_error")
        or state.get("ast_error")
        or (error_history[-1] if error_history else "")
    )

    if retry_count > 0 and last_error:
        system, user = build_self_heal_prompt(
            user_query=query,
            schema_ddl=ddl,
            failed_sql=state.get("generated_sql") or "-- No valid SQL produced in previous attempt",
            error_message=last_error,
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
        max_tokens=600,
        reasoning=False,
    )

    sql = _extract_sql(response.content)
    logger.info("Generated SQL (%s): %s", response.model_used, sql[:200])

    prev_cost = state.get("total_cost_usd", 0.0)

    return {
        "generated_sql": sql,
        "model_used": response.model_used,
        "total_cost_usd": prev_cost + response.cost_usd,
    }
