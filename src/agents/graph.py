from __future__ import annotations

import logging
import time
from typing import Any

from langgraph.graph import END, StateGraph

from src.agents.nodes.analysis_node import analysis_node
from src.agents.nodes.ast_validator import ast_validator_node
from src.agents.nodes.execution_node import execution_node
from src.agents.nodes.guardrail_node import guardrail_node
from src.agents.nodes.heal_node import heal_node
from src.agents.nodes.report_node import report_node
from src.agents.nodes.router_node import router_node
from src.agents.nodes.sql_generator import sql_generator_node
from src.agents.nodes.terminal_error_node import terminal_error_node
from src.agents.nodes.visualizer_node import visualizer_node
from src.agents.state import AgentState

logger = logging.getLogger(__name__)


def _after_guardrail(state: AgentState) -> str:
    if state.get("guardrail_passed", False):
        return "route_query"
    return "terminal_reject"


def _after_ast(state: AgentState) -> str:
    if state.get("ast_valid", False):
        return "execute_sql"

    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)

    if retry_count < max_retries:
        return "heal"
    return "terminal_error"


def _after_execution(state: AgentState) -> str:
    if state.get("execution_error") is None and state.get("query_result") is not None:
        return "generate_visuals"

    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)

    if retry_count < max_retries:
        return "heal"
    return "terminal_error"


def _after_analysis(state: AgentState) -> str:
    if not state.get("sanity_check_passed", True):
        retry_count = state.get("retry_count", 0)
        max_retries = state.get("max_retries", 2)

        if retry_count < max_retries:
            return "heal"

    return "compile_reports"


def _format_final_response(state: AgentState) -> dict[str, Any]:
    parts: list[str] = []

    analysis = state.get("analysis")
    if analysis and analysis.get("summary"):
        parts.append(f"**Summary:** {analysis['summary']}")

        findings = analysis.get("key_findings", [])
        if findings:
            parts.append("\n**Key Findings:**")
            for f in findings:
                parts.append(f"  - {f}")

        anomalies = analysis.get("anomalies", [])
        if anomalies:
            parts.append("\n**[Anomalies]:**")
            for a in anomalies:
                parts.append(f"  - {a}")

    result = state.get("query_result")
    if result and result.get("truncated"):
        parts.append(
            "\n*[Warning] Results were truncated. The full dataset may be larger.*"
        )

    if state.get("ambiguity_flag") and state.get("interpretation_note"):
        parts.append(f"\n*Note: {state['interpretation_note']}*")

    report_paths = state.get("report_paths", {})
    if report_paths:
        parts.append("\n**[Downloads]:**")
        for fmt, path in report_paths.items():
            parts.append(f"  - {fmt.upper()}: `{path}`")

    model = state.get("model_used", "unknown")
    cost = state.get("total_cost_usd", 0.0)
    parts.append(f"\n*Model: {model} | Cost: ${cost:.4f}*")

    return {
        "final_response": "\n".join(parts) if parts else "Analysis complete.",
        "total_latency_ms": (time.perf_counter() * 1000)
    }


def _terminal_reject(state: AgentState) -> dict[str, Any]:
    reason = state.get("guardrail_rejection_reason", "Input blocked by security filters.")
    return {
        "final_response": (
            f"[Security Notice] Your input was blocked by security filters.\n\n"
            f"Reason: {reason}\n\n"
            f"Please rephrase your question as a data analysis query."
        )
    }


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("guardrail", guardrail_node)
    graph.add_node("terminal_reject", _terminal_reject)
    graph.add_node("route_query", router_node)
    graph.add_node("generate_sql", sql_generator_node)
    graph.add_node("validate_ast", ast_validator_node)
    graph.add_node("execute_sql", execution_node)
    graph.add_node("analyze_data", analysis_node)
    graph.add_node("heal", heal_node)
    graph.add_node("terminal_error", terminal_error_node)
    graph.add_node("generate_visuals", visualizer_node)
    graph.add_node("compile_reports", report_node)
    graph.add_node("format_response", _format_final_response)

    graph.set_entry_point("guardrail")

    graph.add_conditional_edges("guardrail", _after_guardrail, {
        "route_query": "route_query",
        "terminal_reject": "terminal_reject",
    })

    graph.add_edge("terminal_reject", END)
    graph.add_edge("route_query", "generate_sql")
    graph.add_edge("generate_sql", "validate_ast")

    graph.add_conditional_edges("validate_ast", _after_ast, {
        "execute_sql": "execute_sql",
        "heal": "heal",
        "terminal_error": "terminal_error",
    })

    graph.add_conditional_edges("execute_sql", _after_execution, {
        "generate_visuals": "generate_visuals",
        "heal": "heal",
        "terminal_error": "terminal_error",
    })

    graph.add_edge("generate_visuals", "analyze_data")

    graph.add_conditional_edges("analyze_data", _after_analysis, {
        "compile_reports": "compile_reports",
        "heal": "heal",
    })

    graph.add_edge("heal", "generate_sql")
    graph.add_edge("terminal_error", END)
    graph.add_edge("compile_reports", "format_response")
    graph.add_edge("format_response", END)

    return graph


def compile_graph():
    graph = build_graph()
    return graph.compile()


def run_query(
    user_query: str,
    compiled_graph=None,
    max_retries: int = 2,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    if compiled_graph is None:
        compiled_graph = compile_graph()

    initial_state: AgentState = {
        "user_query": user_query,
        "retry_count": 0,
        "max_retries": max_retries,
        "error_history": [],
        "total_cost_usd": 0.0,
    }
    if db_path:
        initial_state["db_path"] = db_path

    start = time.perf_counter()
    result = compiled_graph.invoke(initial_state)
    elapsed_ms = (time.perf_counter() - start) * 1000

    result["total_latency_ms"] = round(elapsed_ms, 1)

    logger.info(
        "Query complete: latency=%.0fms, cost=$%.4f, model=%s",
        elapsed_ms,
        result.get("total_cost_usd", 0),
        result.get("model_used", "unknown"),
    )

    return result
