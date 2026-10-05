"""
Enterprise SQL Agent — Streamlit UI.

Multi-tab dashboard with:
    - Query input with routing badge
    - Interactive Plotly chart renderer
    - Data table view
    - PDF/Excel download panel
    - Ambiguity warnings

Usage:
    streamlit run ui/app.py --server.port 8501
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import streamlit as st

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_settings

# ─────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Enterprise SQL Agent",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #16213e;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        color: #666;
        font-size: 1rem;
        margin-bottom: 2rem;
    }
    .routing-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .badge-tier1 {
        background: #d4edda;
        color: #155724;
    }
    .badge-tier2 {
        background: #d1ecf1;
        color: #0c5460;
    }
    .badge-error {
        background: #f8d7da;
        color: #721c24;
    }
    .cost-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.85rem;
        background: #fff3cd;
        color: #856404;
        margin-left: 8px;
    }
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 12px;
        padding: 20px;
        color: white;
        text-align: center;
    }
    .metric-value {
        font-size: 2rem;
        font-weight: 700;
    }
    .metric-label {
        font-size: 0.9rem;
        opacity: 0.9;
    }
    .ambiguity-banner {
        background: #fff3cd;
        border-left: 4px solid #ffc107;
        padding: 12px 16px;
        border-radius: 4px;
        margin-bottom: 16px;
    }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### ⚙️ Configuration")

    settings = get_settings()

    st.markdown(f"**Tier 1:** `{settings.tier1_model}`")
    st.markdown(f"**Tier 2:** `{settings.tier2_model}`")
    st.markdown(f"**DB:** `{settings.database_path.name}`")
    st.divider()

    st.markdown("### 📊 Database Tables")

    # Try to show table list
    try:
        import duckdb
        if settings.database_path.exists():
            conn = duckdb.connect(str(settings.database_path), read_only=True)
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' ORDER BY table_name"
            ).fetchall()
            conn.close()

            for (table_name,) in tables:
                st.markdown(f"  `{table_name}`")
        else:
            st.warning("Database not found. Run `make seed` first.")
    except Exception as e:
        st.error(f"DB error: {e}")

    st.divider()
    st.markdown("### 📈 Session Stats")
    if "query_count" not in st.session_state:
        st.session_state.query_count = 0
        st.session_state.total_cost = 0.0
    st.metric("Queries", st.session_state.query_count)
    st.metric("Total Cost", f"${st.session_state.total_cost:.4f}")


# ─────────────────────────────────────────────────────────────────────
# Main content
# ─────────────────────────────────────────────────────────────────────

st.markdown('<div class="main-header">📊 Enterprise SQL Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">'
    'Ask natural-language questions about your data. '
    'Powered by adaptive model routing with AST-verified sandboxed execution.'
    '</div>',
    unsafe_allow_html=True,
)

# Query input
query = st.text_area(
    "Ask a question about your data",
    placeholder="e.g., What is total revenue by product category? Show me the top 10 customers by order count.",
    height=100,
    key="query_input",
)

col1, col2 = st.columns([1, 5])
with col1:
    run_button = st.button("🚀 Analyze", type="primary", use_container_width=True)
with col2:
    st.markdown("")

if run_button and query.strip():
    st.session_state.query_count += 1

    with st.spinner("🔄 Processing query through the agent pipeline..."):
        start_time = time.perf_counter()

        try:
            from src.agents.graph import run_query
            result = run_query(query.strip())
            elapsed = (time.perf_counter() - start_time) * 1000

        except Exception as e:
            st.error(f"Pipeline error: {e}")
            result = None
            elapsed = 0

    if result:
        # ── Routing badge ────────────────────────────────────────
        col_badge1, col_badge2, col_badge3 = st.columns(3)

        tier = result.get("route_decision", "unknown")
        model = result.get("model_used", "unknown")
        cost = result.get("total_cost_usd", 0.0)
        latency = result.get("total_latency_ms", elapsed)

        st.session_state.total_cost += cost

        badge_class = "badge-tier1" if "TIER_1" in str(tier) else "badge-tier2"
        with col_badge1:
            st.markdown(
                f'<span class="routing-badge {badge_class}">🏷️ {model}</span>'
                f'<span class="cost-badge">💰 ${cost:.4f}</span>',
                unsafe_allow_html=True,
            )
        with col_badge2:
            st.markdown(f"⏱️ **{latency:.0f}ms**")
        with col_badge3:
            retry_count = result.get("retry_count", 0)
            if retry_count > 0:
                st.markdown(f"🔄 **{retry_count} retries**")

        # ── Ambiguity banner ─────────────────────────────────────
        if result.get("ambiguity_flag") and result.get("interpretation_note"):
            st.markdown(
                f'<div class="ambiguity-banner">'
                f'⚠️ {result["interpretation_note"]}'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ── Tabs ─────────────────────────────────────────────────
        tab1, tab2, tab3, tab4, tab5 = st.tabs([
            "📝 Analysis", "📊 Charts", "📋 Data", "💾 Downloads", "🔍 SQL"
        ])

        with tab1:
            st.markdown(result.get("final_response", "No response generated."))

        with tab2:
            chart_specs = result.get("chart_specs", [])
            if chart_specs:
                for spec in chart_specs:
                    fig_dict = spec.get("plotly_figure")
                    if fig_dict:
                        import plotly.graph_objects as go
                        fig = go.Figure(fig_dict)
                        st.plotly_chart(fig, use_container_width=True)
                    elif spec.get("chart_type") == "kpi_card":
                        st.info("📊 KPI Card (metrics displayed in Analysis tab)")
            else:
                st.info("No charts generated for this query.")

        with tab3:
            qr = result.get("query_result")
            if qr and qr.get("parquet_path"):
                try:
                    import pandas as pd
                    df = pd.read_parquet(qr["parquet_path"])
                    st.dataframe(df, use_container_width=True, height=400)
                    st.caption(
                        f"{qr.get('row_count', 0)} rows × "
                        f"{qr.get('column_count', 0)} columns"
                        + (" ⚠️ (truncated)" if qr.get("truncated") else "")
                    )
                except Exception as e:
                    st.error(f"Error loading data: {e}")
            else:
                st.info("No data to display.")

        with tab4:
            report_paths = result.get("report_paths", {})
            if report_paths:
                for fmt, path in report_paths.items():
                    file_path = Path(path)
                    if file_path.exists():
                        with open(file_path, "rb") as f:
                            st.download_button(
                                f"📥 Download {fmt.upper()}",
                                data=f.read(),
                                file_name=file_path.name,
                                mime=(
                                    "application/pdf" if fmt == "pdf"
                                    else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                ),
                            )
                    else:
                        st.warning(f"{fmt.upper()} report not found at {path}")
            else:
                st.info("No reports generated. Reports are created for data queries.")

        with tab5:
            sql = result.get("generated_sql", "No SQL generated")
            st.code(sql, language="sql")

            # Error history
            errors = result.get("error_history", [])
            if errors:
                with st.expander("🔧 Error History (Self-Healing)"):
                    for err in errors:
                        st.warning(err)

elif run_button:
    st.warning("Please enter a question.")
