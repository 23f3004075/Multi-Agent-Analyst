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

_schema_linker: SchemaLinker | None = None
_router: QueryRouter | None = None
_semantic_layer: SemanticLayer | None = None
_inspector: SchemaInspector | None = None


def _get_components() -> tuple[SchemaLinker, QueryRouter, SemanticLayer, SchemaInspector]:
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
    query = state.get("cleaned_query") or state.get("user_query", "")
    logger.info("Routing query: '%s'", query[:80])

    custom_db = state.get("db_path")
    if custom_db:
        from pathlib import Path
        import duckdb
        db_p = Path(custom_db)
        if db_p.exists():
            try:
                conn = duckdb.connect(str(db_p), read_only=True)
                tbl_rows = conn.execute("SHOW TABLES").fetchall()
                user_tables = [r[0] for r in tbl_rows]
                ddl_parts = []
                tables_meta = []
                for tbl in user_tables:
                    col_rows = conn.execute(f"DESCRIBE {tbl}").fetchall()
                    cols_def = ",\n  ".join([f"{col[0]} {col[1]}" for col in col_rows])
                    ddl_parts.append(f"CREATE TABLE {tbl} (\n  {cols_def}\n);")
                    tables_meta.append({
                        "name": tbl,
                        "columns": [{"name": c[0], "data_type": c[1]} for c in col_rows],
                    })

                conn.close()

                from src.schema.column_matcher import ColumnSemanticMatcher
                col_matcher = ColumnSemanticMatcher()
                matches = col_matcher.match_columns(query, tables_meta)
                metric_ctx = col_matcher.format_column_context(matches)

                if user_tables:
                    logger.info("Custom database schema linked: %s", user_tables)
                    return {
                        "route_decision": "TIER_1_SLM",
                        "route_confidence": 1.0,
                        "linked_tables": user_tables,
                        "linked_ddl": "\n\n".join(ddl_parts),
                        "metric_context": metric_ctx,
                        "ambiguity_flag": False,
                        "interpretation_note": f"Custom dataset active with tables: {', '.join(user_tables)}.",
                    }
            except Exception as e:
                logger.warning("Failed inspecting custom db schema: %s", e)

    linker, router, semantic_layer, inspector = _get_components()

    linked_tables = linker.link(query)
    metric_tables = semantic_layer.get_tables_for_metrics(query)
    all_tables = list(dict.fromkeys(linked_tables + metric_tables))

    # Dynamic column matching across all available tables
    from src.schema.column_matcher import ColumnSemanticMatcher
    col_matcher = ColumnSemanticMatcher()
    tables_info = [inspector.get_table_metadata(tbl) for tbl in all_tables]
    col_matches = col_matcher.match_columns(query, tables_info)
    col_context = col_matcher.format_column_context(col_matches)

    linked_ddl = inspector.get_full_ddl(all_tables)
    metric_context = semantic_layer.get_metric_context(query)
    if col_context:
        metric_context = f"{metric_context}\n\n{col_context}".strip()
    decision = router.route(query, linked_tables=all_tables)

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
