"""
Router Node — Schema Linking + Complexity Routing.

Links the query to relevant tables, retrieves metric context from the
semantic layer, and routes to the appropriate model tier.
"""

from __future__ import annotations

import logging
from typing import Any

from src.agents.state import AgentState
from src.config import get_settings
from src.database.connection import create_secure_connection
from src.database.schema_inspector import SchemaInspector
from src.router.router import QueryRouter
from src.schema.linker import SchemaLinker
from src.schema.semantic_layer import SemanticLayer

logger = logging.getLogger(__name__)

# Module-level singletons (initialized lazily)
_schema_linker: SchemaLinker | None = None
_router: QueryRouter | None = None
_semantic_layer: SemanticLayer | None = None
_inspector: SchemaInspector | None = None


def _get_components() -> tuple[SchemaLinker, QueryRouter, SemanticLayer, SchemaInspector]:
    """Lazy-initialize shared components."""
    global _schema_linker, _router, _semantic_layer, _inspector

    settings = get_settings()

    if _inspector is None:
        conn = create_secure_connection(settings)
        _inspector = SchemaInspector(conn)

    if _schema_linker is None:
        table_texts = _inspector.get_embedding_texts()
        _schema_linker = SchemaLinker(settings, table_texts=table_texts)

    if _router is None:
        _router = QueryRouter(settings)

    if _semantic_layer is None:
        _semantic_layer = SemanticLayer()

    return _schema_linker, _router, _semantic_layer, _inspector


def router_node(state: AgentState) -> dict[str, Any]:
    """
    Link schema, resolve metrics, and route the query.

    Produces: route_decision, route_confidence, linked_tables,
              linked_ddl, metric_context, ambiguity_flag,
              interpretation_note
    """
    query = state.get("cleaned_query") or state.get("user_query", "")
    logger.info("Routing query: '%s'", query[:80])

    linker, router, semantic_layer, inspector = _get_components()

    # Step 1: Schema linking — retrieve relevant tables
    linked_tables = linker.link(query)

    # Step 2: Augment with tables needed for referenced metrics
    metric_tables = semantic_layer.get_tables_for_metrics(query)
    all_tables = list(dict.fromkeys(linked_tables + metric_tables))  # Dedupe, preserve order

    # Step 3: Get DDL for linked tables
    linked_ddl = inspector.get_full_ddl(all_tables)

    # Step 4: Get metric context
    metric_context = semantic_layer.get_metric_context(query)

    # Step 5: Route
    decision = router.route(query, linked_tables=all_tables)

    # Step 6: Generate interpretation note if ambiguous
    interpretation_note = None
    if decision.ambiguous:
        interpretation_note = (
            f"This query may be ambiguous. Routing to {decision.tier.value} "
            f"with confidence {decision.confidence:.0%}. "
            f"Tables used: {', '.join(all_tables)}."
        )

    return {
        "route_decision": decision.tier.value,
        "route_confidence": decision.confidence,
        "linked_tables": all_tables,
        "linked_ddl": linked_ddl,
        "metric_context": metric_context,
        "ambiguity_flag": decision.ambiguous,
        "interpretation_note": interpretation_note,
    }
