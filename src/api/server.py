"""
FastAPI Server for Enterprise SQL Agent.

Serves:
    - POST /api/query: Executes natural-language queries through the LangGraph pipeline
    - GET /api/health: Database connectivity & table inventory
    - GET /api/download: Download generated PDF / Excel reports
    - Static files: Serves the HTML + CSS + Vue.js / Bootstrap 5 UI
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import duckdb
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agents.graph import compile_graph, run_query
from src.config import get_settings

logger = logging.getLogger(__name__)

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent.parent
WEB_DIR = BASE_DIR / "web"

app = FastAPI(
    title="Enterprise SQL Agent API",
    description="Enterprise NL-to-SQL & Autonomous BI Engine API",
    version="1.0.0",
)

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compile graph once at server startup
_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        logger.info("Compiling LangGraph state machine...")
        _compiled_graph = compile_graph()
    return _compiled_graph


@app.on_event("startup")
def prewarm_models():
    """Pre-warm LangGraph machine and embedding models to eliminate first-query latency."""
    import threading

    def _warmup():
        try:
            logger.info("Pre-warming graph compiler and router components...")
            get_graph()
            from src.agents.nodes.router_node import _get_components
            _get_components()
            logger.info("System components pre-warmed successfully.")
        except Exception as e:
            logger.warning("Startup pre-warming encountered: %s", e)

    threading.Thread(target=_warmup, daemon=True).start()


# Request / Response Schemas
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="Natural-language question")
    max_retries: int = Field(default=2, ge=0, le=5, description="Maximum self-healing retries")


def _strip_emojis(text: str) -> str:
    """Remove emojis from strings for strict no-emoji UI requirements."""
    if not text:
        return ""
    # Remove emoji Unicode ranges
    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff\u2600-\u26ff\u2700-\u27bf\u2300-\u23ff\u2b50-\u2b55\u200d\ufe0f]",
        flags=re.UNICODE,
    )
    cleaned = emoji_pattern.sub("", text)
    # Replace markdown warning symbols if any
    cleaned = cleaned.replace("⚠️", "[Warning]").replace("⛔", "[Blocked]").replace("📄", "[Report]")
    return cleaned.strip()


def _make_json_safe(data: Any) -> Any:
    """Recursively convert NumPy types, Pandas objects, and unknown types to JSON primitives."""
    return json.loads(
        json.dumps(
            data,
            default=lambda o: (
                o.tolist() if hasattr(o, "tolist")
                else o.isoformat() if hasattr(o, "isoformat")
                else str(o)
            ),
        )
    )


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    """Check database status, table catalog, and active configuration."""
    settings = get_settings()
    db_path = Path(settings.database_path)

    if not db_path.exists():
        return {
            "status": "degraded",
            "database_exists": False,
            "database_path": str(db_path),
            "tables": [],
            "message": "Database not found. Please run: python data/seed_olist.py",
        }

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
        table_names = [t[0] for t in tables]
        conn.close()

        return {
            "status": "healthy",
            "database_exists": True,
            "database_path": str(db_path),
            "tables": table_names,
            "table_count": len(table_names),
        }
    except Exception as e:
        return {
            "status": "error",
            "database_exists": True,
            "database_path": str(db_path),
            "tables": [],
            "error": str(e),
        }


_tables_preview_cache: dict[str, Any] | None = None


@app.get("/api/tables/preview")
def get_tables_preview() -> dict[str, Any]:
    """
    Return catalog of all database tables with total row counts, column lists,
    and a 5-row sample preview for interactive hover inspection.
    """
    global _tables_preview_cache
    if _tables_preview_cache is not None:
        return _tables_preview_cache

    settings = get_settings()
    db_path = Path(settings.database_path)

    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Database not found.")

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        tables = conn.execute("SHOW TABLES").fetchall()
        table_names = [t[0] for t in tables]

        previews = []
        for name in table_names:
            cnt = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            df = conn.execute(f"SELECT * FROM {name} LIMIT 5").df()
            columns = list(df.columns)
            rows = df.head(5).to_dict(orient="records")

            previews.append({
                "name": name,
                "row_count": cnt,
                "column_count": len(columns),
                "columns": columns,
                "sample_rows": rows,
            })

        conn.close()
        result = _make_json_safe({
            "tables": previews,
            "total_tables": len(previews),
        })
        _tables_preview_cache = result
        return result
    except Exception as e:
        logger.exception("Failed generating table previews: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed generating table previews: {e}")


@app.post("/api/query")
def execute_query(req: QueryRequest) -> dict[str, Any]:
    """Execute natural-language query through the full agent pipeline."""
    graph = get_graph()

    try:
        raw_state = run_query(
            user_query=req.query,
            compiled_graph=graph,
            max_retries=req.max_retries,
        )
    except Exception as e:
        logger.exception("Error executing agent graph")
        raise HTTPException(status_code=500, detail=f"Pipeline execution error: {e}")

    # Process query results preview
    data_preview: list[dict[str, Any]] = []
    columns: list[str] = []
    row_count = 0
    truncated = False

    query_res = raw_state.get("query_result")
    if query_res and isinstance(query_res, dict):
        row_count = query_res.get("row_count", 0)
        truncated = query_res.get("truncated", False)
        schema_dict = query_res.get("schema", {})
        columns = list(schema_dict.keys()) if isinstance(schema_dict, dict) else []

        parquet_path = query_res.get("parquet_path")
        if parquet_path and os.path.exists(parquet_path):
            try:
                df = pd.read_parquet(parquet_path)
                columns = list(df.columns)
                # Take up to 100 rows for UI table
                preview_df = df.head(100)
                data_preview = preview_df.to_dict(orient="records")
            except Exception as e:
                logger.warning("Failed to read parquet preview: %s", e)

    # Process chart spec and Plotly figures
    chart_specs = raw_state.get("chart_specs", [])
    chart_figure = None
    chart_spec = None
    if chart_specs and len(chart_specs) > 0:
        # Default or first chart spec
        default_specs = [s for s in chart_specs if s.get("is_default")]
        chart_spec = default_specs[0] if default_specs else chart_specs[0]
        if isinstance(chart_spec, dict) and "plotly_figure" in chart_spec:
            chart_figure = chart_spec.get("plotly_figure")

    # Format response without emojis
    raw_response = raw_state.get("final_response", "")
    clean_response = _strip_emojis(raw_response)

    # Clean error history
    error_history = [_strip_emojis(err) for err in raw_state.get("error_history", [])]

    # Clean report paths
    raw_reports = raw_state.get("report_paths", {})
    report_links = {}
    if isinstance(raw_reports, dict):
        for fmt, path in raw_reports.items():
            if path and os.path.exists(path):
                report_links[fmt] = f"/api/download?path={path}"
                if fmt == "pdf":
                    report_links["pdf_inline"] = f"/api/download?path={path}&inline=true"

    payload = {
        "success": raw_state.get("guardrail_passed", True) and not raw_state.get("execution_error"),
        "guardrail_passed": raw_state.get("guardrail_passed", True),
        "guardrail_rejection_reason": _strip_emojis(raw_state.get("guardrail_rejection_reason", "")),
        "route_decision": raw_state.get("route_decision", "TIER_1_SLM"),
        "route_confidence": raw_state.get("route_confidence", 1.0),
        "model_used": raw_state.get("model_used", "N/A"),
        "generated_sql": raw_state.get("generated_sql", ""),
        "final_response": clean_response,
        "total_cost_usd": raw_state.get("total_cost_usd", 0.0),
        "total_latency_ms": raw_state.get("total_latency_ms", 0.0),
        "retry_count": raw_state.get("retry_count", 0),
        "error_history": error_history,
        "ambiguity_flag": raw_state.get("ambiguity_flag", False),
        "interpretation_note": _strip_emojis(raw_state.get("interpretation_note", "")),
        "chart_spec": chart_spec,
        "chart_figure": chart_figure,
        "chart_options": chart_specs,
        "data": {
            "columns": columns,
            "rows": data_preview,
            "total_rows": row_count,
            "truncated": truncated,
        },
        "reports": report_links,
    }
    return _make_json_safe(payload)


@app.post("/api/query/stream")
def execute_query_stream(req: QueryRequest):
    """
    Stream natural-language query execution progress and progressive results.

    Emits SSE events as each component finishes:
      - sql: Generated SQL statement and audit
      - data: Data Preview (10 Rows)
      - chart: Interactive Chart options & figures
      - summary: Executive Summary & findings
      - reports: PDF & Excel report downloads
      - complete: Overall execution summary
    """
    import queue
    import threading
    import time
    from fastapi.responses import StreamingResponse

    event_queue: queue.Queue = queue.Queue()

    def run_worker():
        graph = get_graph()
        initial_state = {
            "user_query": req.query,
            "retry_count": 0,
            "max_retries": req.max_retries,
            "error_history": [],
            "total_cost_usd": 0.0,
        }

        start_time = time.perf_counter()
        accumulated_state: dict[str, Any] = dict(initial_state)

        try:
            event_queue.put({
                "event": "status",
                "message": "Validating question against security filters and schema...",
            })

            # Stream through LangGraph nodes
            for step_event in graph.stream(initial_state, stream_mode="updates"):
                node_name = list(step_event.keys())[0] if step_event else ""
                node_output = step_event.get(node_name, {})
                accumulated_state.update(node_output)

                logger.info("Stream progress node: %s", node_name)

                # ── Guardrail check ──────────────────────────────────
                if node_name == "guardrail":
                    if not node_output.get("guardrail_passed", True):
                        event_queue.put({
                            "event": "guardrail_rejected",
                            "reason": _strip_emojis(node_output.get("guardrail_rejection_reason", "Security policy violation")),
                        })
                        break

                # ── Terminal reject ──────────────────────────────────
                elif node_name == "terminal_reject":
                    event_queue.put({
                        "event": "guardrail_rejected",
                        "reason": _strip_emojis(accumulated_state.get("guardrail_rejection_reason", "Query blocked")),
                    })
                    break

                # ── SQL Generation & AST Validation ──────────────────
                elif node_name in ("generate_sql", "validate_ast"):
                    sql = accumulated_state.get("generated_sql")
                    if sql:
                        event_queue.put({
                            "event": "stage_update",
                            "stage": "sql",
                            "status": "done",
                            "data": {
                                "generated_sql": sql,
                                "model_used": accumulated_state.get("model_used", "N/A"),
                                "route_decision": accumulated_state.get("route_decision", "TIER_1_SLM"),
                                "route_confidence": accumulated_state.get("route_confidence", 1.0),
                                "error_history": [_strip_emojis(e) for e in accumulated_state.get("error_history", [])],
                            },
                        })

                # ── SQL Execution -> Data Preview ────────────────────
                elif node_name == "execute_sql":
                    exec_err = node_output.get("execution_error")
                    if exec_err:
                        event_queue.put({
                            "event": "stage_update",
                            "stage": "data",
                            "status": "failed",
                            "error": _strip_emojis(exec_err),
                        })
                    else:
                        q_res = node_output.get("query_result") or {}
                        parquet_path = q_res.get("parquet_path")
                        columns = []
                        preview_rows = []
                        total_rows = q_res.get("row_count", 0)
                        truncated = q_res.get("truncated", False)

                        if parquet_path and os.path.exists(parquet_path):
                            try:
                                df = pd.read_parquet(parquet_path)
                                columns = list(df.columns)
                                preview_rows = df.head(10).to_dict(orient="records")
                            except Exception as pe:
                                logger.warning("Stream failed reading parquet: %s", pe)
                        elif q_res.get("sample_rows"):
                            preview_rows = q_res["sample_rows"][:10]
                            columns = list(preview_rows[0].keys()) if preview_rows else []

                        event_queue.put({
                            "event": "stage_update",
                            "stage": "data",
                            "status": "done",
                            "data": {
                                "columns": columns,
                                "rows": preview_rows,
                                "total_rows": total_rows,
                                "truncated": truncated,
                            },
                        })

                # ── Visualization -> Multi-chart Options ─────────────
                elif node_name == "generate_visuals":
                    chart_specs = node_output.get("chart_specs", [])
                    default_spec = None
                    default_fig = None
                    if chart_specs:
                        defaults = [s for s in chart_specs if s.get("is_default")]
                        default_spec = defaults[0] if defaults else chart_specs[0]
                        default_fig = default_spec.get("plotly_figure")

                    event_queue.put({
                        "event": "stage_update",
                        "stage": "chart",
                        "status": "done" if chart_specs else "done",
                        "data": {
                            "chart_options": chart_specs,
                            "chart_spec": default_spec,
                            "chart_figure": default_fig,
                        },
                    })

                # ── Analysis -> Executive Summary ────────────────────
                elif node_name == "analyze_data":
                    analysis = node_output.get("analysis") or {}
                    summary = analysis.get("summary", "")
                    findings = analysis.get("key_findings", [])
                    anomalies = analysis.get("anomalies", [])
                    interp_note = accumulated_state.get("interpretation_note", "")

                    event_queue.put({
                        "event": "stage_update",
                        "stage": "summary",
                        "status": "done",
                        "data": {
                            "summary": _strip_emojis(summary),
                            "key_findings": [_strip_emojis(f) for f in findings],
                            "anomalies": [_strip_emojis(a) for a in anomalies],
                            "interpretation_note": _strip_emojis(interp_note),
                        },
                    })

                # ── Report Generation -> Export Reports ──────────────
                elif node_name == "compile_reports":
                    r_paths = node_output.get("report_paths") or {}
                    rep_links = {}
                    for fmt, path in r_paths.items():
                        if path and os.path.exists(path):
                            rep_links[fmt] = f"/api/download?path={path}"
                            if fmt == "pdf":
                                rep_links["pdf_inline"] = f"/api/download?path={path}&inline=true"

                    event_queue.put({
                        "event": "stage_update",
                        "stage": "reports",
                        "status": "done",
                        "data": {
                            "reports": rep_links,
                            "error": node_output.get("report_error"),
                        },
                    })

                # ── Final Response ───────────────────────────────────
                elif node_name == "format_response":
                    clean_resp = _strip_emojis(node_output.get("final_response", ""))
                    event_queue.put({
                        "event": "final_response_ready",
                        "final_response": clean_resp,
                    })

            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 1)
            event_queue.put({
                "event": "complete",
                "total_latency_ms": elapsed_ms,
                "total_cost_usd": accumulated_state.get("total_cost_usd", 0.0),
                "retry_count": accumulated_state.get("retry_count", 0),
                "model_used": accumulated_state.get("model_used", "N/A"),
                "guardrail_passed": accumulated_state.get("guardrail_passed", True),
            })

        except Exception as e:
            logger.exception("Error during streaming pipeline execution")
            event_queue.put({
                "event": "error",
                "detail": str(e),
            })
        finally:
            event_queue.put(None)  # Sentinel to close stream

    # Launch worker thread
    threading.Thread(target=run_worker, daemon=True).start()

    def event_generator():
        while True:
            item = event_queue.get()
            if item is None:
                break
            safe_item = _make_json_safe(item)
            yield f"data: {json.dumps(safe_item)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download")
def download_report(
    path: str = Query(..., description="Report file path on server"),
    inline: bool = Query(default=False, description="Display inline in browser for PDF preview"),
):
    """Download or preview generated PDF / Excel report."""
    file_path = Path(path).resolve()

    # Safety check: Ensure path exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Requested file not found.")

    filename = file_path.name
    is_pdf = file_path.suffix.lower() == ".pdf"
    media_type = "application/pdf" if is_pdf else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    if inline and is_pdf:
        # Serve inline so browser can display inside iframe / preview loader
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Content-Type": "application/pdf",
            },
        )

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=media_type,
    )


# Mount static directory for HTML UI
if not WEB_DIR.exists():
    WEB_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")

