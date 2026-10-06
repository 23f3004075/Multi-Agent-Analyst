"""
Guardrail Node — Input Security Check.

Runs the lightweight regex pre-filter and NeMo Guardrails in sequence.
If either rejects the input, transitions directly to terminal_reject.
"""

from __future__ import annotations

import logging
from typing import Any

from src.agents.state import AgentState
from src.guardrails.input_sanitizer import sanitize_input

logger = logging.getLogger(__name__)


def guardrail_node(state: AgentState) -> dict[str, Any]:
    """
    Check user input for injection and safety violations.

    Returns updated state fields for guardrail_passed and cleaned_query.
    """
    user_query = state["user_query"]
    logger.info("Guardrail check: '%s'", user_query[:80])

    sanitization = sanitize_input(user_query)

    if not sanitization.is_safe:
        logger.warning(
            "Input blocked by sanitizer: %s — %s",
            sanitization.threat_category,
            sanitization.explanation,
        )
        return {
            "guardrail_passed": False,
            "guardrail_rejection_reason": (
                f"Input blocked: {sanitization.explanation}"
            ),
            "cleaned_query": user_query,
        }

    return {
        "guardrail_passed": True,
        "guardrail_rejection_reason": None,
        "cleaned_query": sanitization.cleaned_text,
    }
