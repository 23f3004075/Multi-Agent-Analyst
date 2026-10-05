"""
Analysis Node — Data Interpretation and Sanity Checks.

Computes summary statistics, detects anomalies, performs sanity
checks, and generates a structured analysis via LLM.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.agents.state import AgentState
from src.config import get_settings
from src.guardrails.input_sanitizer import sanitize_data_for_llm
from src.llm.client import LLMClient
from src.llm.prompts.sql_generation import build_analysis_prompt

logger = logging.getLogger(__name__)

_llm_client: LLMClient | None = None


def _get_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(get_settings())
    return _llm_client


def analysis_node(state: AgentState) -> dict[str, Any]:
    """
    Analyze query results: sanity checks + LLM-powered interpretation.

    Produces: analysis, sanity_check_passed, sanity_check_warning
    """
    result = state.get("query_result")

    if not result:
        return {
            "analysis": None,
            "sanity_check_passed": False,
            "sanity_check_warning": "No query results to analyze",
        }

    # ── Sanity checks ────────────────────────────────────────────────
    sanity_passed, sanity_warning = _run_sanity_checks(result)

    if not sanity_passed:
        logger.warning("Sanity check failed: %s", sanity_warning)

    # ── LLM-powered analysis ─────────────────────────────────────────
    try:
        llm_context = sanitize_data_for_llm(result.get("llm_context", ""))
        system, user = build_analysis_prompt(
            user_query=state["cleaned_query"],
            sql_query=state.get("generated_sql", ""),
            result_context=llm_context,
        )

        client = _get_client()
        response = client.generate(
            prompt=user,
            system=system,
            model_tier="tier2",  # Always use frontier for analysis quality
            temperature=0.0,
            max_tokens=512,
            json_mode=True,
            reasoning=False,
        )

        analysis = json.loads(response.content)
        prev_cost = state.get("total_cost_usd", 0.0)

        return {
            "analysis": analysis,
            "sanity_check_passed": sanity_passed,
            "sanity_check_warning": sanity_warning,
            "total_cost_usd": prev_cost + response.cost_usd,
        }

    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Analysis LLM failed: %s", e)
        # Graceful degradation: return basic analysis without LLM
        return {
            "analysis": {
                "summary": f"Query returned {result.get('row_count', 0)} rows.",
                "key_findings": [],
                "anomalies": [],
                "suggested_chart": {},
                "interpretation_note": None,
            },
            "sanity_check_passed": sanity_passed,
            "sanity_check_warning": sanity_warning,
        }


def _run_sanity_checks(result: dict) -> tuple[bool, str | None]:
    """
    Run basic sanity checks on query results.

    Catches: zero rows, all NULLs, absurd magnitudes.
    Returns: (passed, warning_message)
    """
    row_count = result.get("row_count", 0)

    # Check 1: Zero rows
    if row_count == 0:
        return False, "Query returned zero rows — the filter may be too restrictive."

    # Check 2: All NULL values in sample
    sample = result.get("sample_rows", [])
    if sample:
        first_row = sample[0]
        if all(v is None for v in first_row.values()):
            return False, "All values in the first row are NULL — possible column mismatch."

    # Check 3: Absurd magnitudes in numeric stats
    stats = result.get("summary_stats", {})
    for col, col_stats in stats.items():
        if isinstance(col_stats, dict) and "max" in col_stats:
            max_val = col_stats.get("max", 0)
            min_val = col_stats.get("min", 0)
            if isinstance(max_val, (int, float)) and abs(max_val) > 1e15:
                return (
                    False,
                    f"Column '{col}' has an extreme value ({max_val}) — "
                    f"possible Cartesian join or calculation error.",
                )

    return True, None
