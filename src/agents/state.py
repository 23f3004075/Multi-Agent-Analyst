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

    chart_type: str
    title: str
    x_axis: Optional[str]
    y_axis: Optional[str]
    group_by: Optional[str]


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

    user_query: str
    cleaned_query: str
    conversation_id: str

    guardrail_passed: bool
    guardrail_rejection_reason: Optional[str]

    route_decision: str
    route_confidence: float
    linked_tables: list[str]
    linked_ddl: str
    metric_context: str

    generated_sql: Optional[str]
    model_used: str

    ast_valid: bool
    ast_error: Optional[str]

    query_result: Optional[dict[str, Any]]
    execution_error: Optional[str]

    retry_count: int
    max_retries: int
    error_history: list[str]

    analysis: Optional[AnalysisResult]
    sanity_check_passed: bool
    sanity_check_warning: Optional[str]

    chart_specs: list[ChartSpec]
    chart_render_error: Optional[str]

    report_paths: dict[str, str]
    report_error: Optional[str]

    final_response: str
    ambiguity_flag: bool
    interpretation_note: Optional[str]

    total_cost_usd: float
    total_latency_ms: float
