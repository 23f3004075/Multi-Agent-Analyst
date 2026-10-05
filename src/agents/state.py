"""
LangGraph Agent State Definition.

Central typed state schema for the entire agent workflow. Every node
reads from and writes to this state. Designed for:
    - Small checkpoint size (~2 KB, not 50 MB)
    - Clear data contracts between nodes
    - Explicit error tracking and retry state
    - Hybrid clarification support (ambiguity_flag + interpretation_note)

All raw data is stored as Parquet file references (QueryResult),
NOT as raw DataFrames in the state dictionary.
"""

from __future__ import annotations

from typing import Any, Optional

from typing_extensions import TypedDict


class ChartSpec(TypedDict, total=False):
    """Minimal chart specification — LLM outputs this, code builds the figure."""

    chart_type: str  # "bar" | "line" | "scatter" | "pie" | "kpi_card"
    title: str
    x_axis: Optional[str]  # Column name
    y_axis: Optional[str]  # Column name
    group_by: Optional[str]  # Column name for color/grouping


class AnalysisResult(TypedDict, total=False):
    """Structured analysis output from the analysis node."""

    summary: str
    key_findings: list[str]
    anomalies: list[str]
    suggested_chart: ChartSpec
    interpretation_note: Optional[str]


class AgentState(TypedDict, total=False):
    """
    Typed state for the LangGraph agent workflow.

    Every field is documented with its producer node and consumer nodes.
    Optional fields use total=False to allow incremental state building.
    """

    # ── Input (produced by: user, consumed by: all nodes) ────────────
    user_query: str  # Raw user question
    cleaned_query: str  # After input sanitization
    conversation_id: str  # For tracing and cache keying

    # ── Guardrails (produced by: guardrail_node) ─────────────────────
    guardrail_passed: bool  # Whether input passed safety checks
    guardrail_rejection_reason: Optional[str]  # Why input was rejected

    # ── Routing (produced by: router_node, consumed by: sql_generator)
    route_decision: str  # "TIER_1_SLM" | "TIER_2_FRONTIER"
    route_confidence: float  # 0.0–1.0, used for hybrid clarification
    linked_tables: list[str]  # Schema linker output (table names)
    linked_ddl: str  # DDL for linked tables only
    metric_context: str  # Semantic layer context for the prompt

    # ── SQL Generation (produced by: sql_generator) ──────────────────
    generated_sql: Optional[str]  # The SQL query
    model_used: str  # Actual model ID used

    # ── AST Validation (produced by: ast_validator) ──────────────────
    ast_valid: bool  # Whether SQL passed AST allowlist
    ast_error: Optional[str]  # AST validation error message

    # ── Execution (produced by: execution_node) ──────────────────────
    # NOTE: We store a serializable dict, not the QueryResult dataclass,
    # because LangGraph state must be JSON-serializable.
    query_result: Optional[dict[str, Any]]  # QueryResult as dict (~2 KB)
    execution_error: Optional[str]  # Runtime execution error

    # ── Self-Healing (produced by: heal_node) ────────────────────────
    retry_count: int  # Current retry attempt (0-indexed)
    max_retries: int  # Maximum retries (default: 2)
    error_history: list[str]  # Stack traces for escalation context

    # ── Analysis (produced by: analysis_node) ────────────────────────
    analysis: Optional[AnalysisResult]  # Structured analysis output
    sanity_check_passed: bool  # Zero rows, all NULLs, etc.
    sanity_check_warning: Optional[str]

    # ── Visualization (produced by: visualizer_node) ─────────────────
    chart_specs: list[ChartSpec]  # Validated chart specifications
    chart_render_error: Optional[str]

    # ── Reports (produced by: report_node) ───────────────────────────
    report_paths: dict[str, str]  # {"pdf": "/path", "excel": "/path"}
    report_error: Optional[str]

    # ── Output (produced by: format_response) ────────────────────────
    final_response: str  # The formatted response text
    ambiguity_flag: bool  # True if route_confidence < threshold
    interpretation_note: Optional[str]  # "I interpreted 'revenue' as SUM(price)"

    # ── Cost tracking ────────────────────────────────────────────────
    total_cost_usd: float  # Cumulative LLM cost for this query
    total_latency_ms: float  # End-to-end latency
