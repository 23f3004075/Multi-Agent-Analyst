from __future__ import annotations

import logging
from typing import Any

from src.agents.state import AgentState

logger = logging.getLogger(__name__)


def terminal_error_node(state: AgentState) -> dict[str, Any]:
    error_history = state.get("error_history", [])
    query = state.get("cleaned_query", "your question")
    retry_count = state.get("retry_count", 0)

    last_error = error_history[-1] if error_history else "Unknown error"

    response = (
        f"I wasn't able to generate a valid SQL query for: \"{query}\"\n\n"
        f"After {retry_count} attempt(s), the last error was:\n"
        f"  {last_error}\n\n"
        f"This might be because:\n"
        f"  • The question requires tables or columns not in the database\n"
        f"  • The question is ambiguous and needs clarification\n"
        f"  • The query is too complex for the current models\n\n"
        f"Please try rephrasing your question or being more specific."
    )

    logger.warning("Terminal error: %d retries exhausted for '%s'", retry_count, query[:60])

    return {"final_response": response}
