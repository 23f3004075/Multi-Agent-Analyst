"""
Visualizer Node — Chart Generation from Query Results.

Takes analysis output (with suggested chart spec) and builds
Plotly figures deterministically from minimal specifications.
Falls back to rule-based chart type selection if LLM suggestion fails.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

import re
from typing import Any

import pandas as pd

from src.agents.state import AgentState, ChartSpec

logger = logging.getLogger(__name__)

# Premium modern color sequence for Plotly charts
COLOR_PALETTE = [
    "#2563eb",  # Royal Blue
    "#06b6d4",  # Cyan
    "#8b5cf6",  # Violet
    "#ec4899",  # Pink
    "#f59e0b",  # Amber
    "#10b981",  # Emerald
    "#6366f1",  # Indigo
    "#f97316",  # Orange
    "#14b8a6",  # Teal
]


def _detect_prompt_preference(query: str) -> str | None:
    """Detect if the user explicitly asked for a specific chart type in their query."""
    q = query.lower()
    if re.search(r"\b(horizontal|hbar|barh)\b", q):
        return "horizontal_bar"
    if re.search(r"\b(pie|donut|doughnut)\b", q):
        return "pie"
    if re.search(r"\b(line|trend|trajectory|timeline)\b", q):
        return "line"
    if re.search(r"\b(area|volume)\b", q):
        return "area"
    if re.search(r"\b(treemap|tree map)\b", q):
        return "treemap"
    if re.search(r"\b(scatter|correlation|dot)\b", q):
        return "scatter"
    if re.search(r"\b(bar|column|histogram)\b", q):
        return "bar"
    return None


def _build_single_figure(
    df: pd.DataFrame,
    chart_type: str,
    x: str | None,
    y: str | None,
    group: str | None = None,
    title: str = "Chart",
) -> dict | None:
    """Build an individual styled Plotly figure."""
    import plotly.express as px

    try:
        # Clone df preview up to 50 rows for snappy rendering
        plot_df = df.head(50).copy()

        if chart_type == "bar":
            fig = px.bar(
                plot_df,
                x=x,
                y=y,
                color=group or x,
                title=title,
                color_discrete_sequence=COLOR_PALETTE,
            )
            fig.update_layout(showlegend=bool(group))

        elif chart_type == "horizontal_bar":
            # Sort for clean horizontal presentation
            if y and y in plot_df.columns:
                sorted_df = plot_df.sort_values(by=y, ascending=True)
            else:
                sorted_df = plot_df
            fig = px.bar(
                sorted_df,
                x=y,
                y=x,
                orientation="h",
                color=group or x,
                title=title,
                color_discrete_sequence=COLOR_PALETTE,
            )
            fig.update_layout(showlegend=False)

        elif chart_type == "line":
            fig = px.line(
                plot_df,
                x=x,
                y=y,
                color=group,
                markers=True,
                title=title,
                color_discrete_sequence=COLOR_PALETTE,
            )

        elif chart_type == "area":
            fig = px.area(
                plot_df,
                x=x,
                y=y,
                color=group,
                title=title,
                color_discrete_sequence=COLOR_PALETTE,
            )

        elif chart_type == "pie":
            # For donut/pie, limit to top 10 categories
            pie_df = plot_df.head(10)
            fig = px.pie(
                pie_df,
                names=x,
                values=y,
                title=title,
                hole=0.45,
                color_discrete_sequence=COLOR_PALETTE,
            )
            fig.update_traces(textposition="inside", textinfo="percent+label")

        elif chart_type == "treemap":
            treemap_df = plot_df.head(20)
            fig = px.treemap(
                treemap_df,
                path=[x],
                values=y,
                title=title,
                color=y,
                color_continuous_scale="Blues",
            )

        elif chart_type == "scatter":
            fig = px.scatter(
                plot_df,
                x=x,
                y=y,
                color=group,
                title=title,
                color_discrete_sequence=COLOR_PALETTE,
                size=y if y and pd.api.types.is_numeric_dtype(plot_df[y]) and (plot_df[y] > 0).all() else None,
            )

        else:
            fig = px.bar(plot_df, x=x, y=y, title=title, color_discrete_sequence=COLOR_PALETTE)

        fig.update_layout(
            template="plotly_white",
            font=dict(family="Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif", size=12),
            margin=dict(l=50, r=40, t=60, b=50),
            hovermode="closest",
            autosize=True,
        )

        return fig.to_dict()

    except Exception as e:
        logger.debug("Failed building %s figure: %s", chart_type, e)
        return None


def _generate_all_chart_options(
    df: pd.DataFrame,
    user_query: str = "",
    suggested_spec: dict | None = None,
) -> list[ChartSpec]:
    """Generate multiple viable chart options tailored to the data and user prompt."""
    if df.empty or len(df.columns) == 0:
        return []

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    datetime_cols = df.select_dtypes(include=["datetime", "datetimetz"]).columns.tolist()
    object_cols = df.select_dtypes(include=["object", "category", "string"]).columns.tolist()

    # Detect string columns that parse as dates
    for col in list(object_cols):
        sample = df[col].dropna().head(5)
        if len(sample) > 0:
            try:
                pd.to_datetime(sample)
                datetime_cols.append(col)
                object_cols.remove(col)
            except (ValueError, TypeError):
                pass

    # Single aggregate row / KPI
    if len(df) == 1 and len(df.columns) <= 4:
        return [
            ChartSpec(
                chart_type="kpi_card",
                label="KPI Summary",
                title="Key Metrics",
                is_default=True,
            )
        ]

    # Primary dimensions
    primary_x = None
    primary_y = None
    group_col = None

    if datetime_cols and numeric_cols:
        primary_x = datetime_cols[0]
        primary_y = numeric_cols[0]
        group_col = object_cols[0] if object_cols else None
    elif object_cols and numeric_cols:
        primary_x = object_cols[0]
        primary_y = numeric_cols[0]
        group_col = object_cols[1] if len(object_cols) > 1 else None
    elif len(numeric_cols) >= 2:
        primary_x = numeric_cols[0]
        primary_y = numeric_cols[1]
    elif len(df.columns) >= 2:
        primary_x = df.columns[0]
        primary_y = df.columns[1]

    # Detect prompt preferences
    prompt_pref = _detect_prompt_preference(user_query)

    # Determine default chart type
    if prompt_pref:
        default_type = prompt_pref
    elif datetime_cols:
        default_type = "line"
    elif object_cols and df[object_cols[0]].nunique() <= 8 and len(df) <= 12:
        default_type = "pie"
    else:
        default_type = "bar"

    # Build candidates
    chart_candidates = []

    # 1. Vertical Bar Chart
    if primary_x and primary_y:
        chart_candidates.append({
            "type": "bar",
            "label": "Bar Chart",
            "title": f"{primary_y} by {primary_x}",
            "x": primary_x,
            "y": primary_y,
            "group": group_col,
        })

    # 2. Horizontal Bar Chart (especially great for categories)
    if object_cols and primary_y:
        chart_candidates.append({
            "type": "horizontal_bar",
            "label": "Horizontal Bar",
            "title": f"Ranked {primary_y} by {primary_x}",
            "x": primary_x,
            "y": primary_y,
            "group": group_col,
        })

    # 3. Line Chart
    if (datetime_cols or len(df) > 3) and primary_x and primary_y:
        chart_candidates.append({
            "type": "line",
            "label": "Line Trend",
            "title": f"{primary_y} Trend over {primary_x}",
            "x": primary_x,
            "y": primary_y,
            "group": group_col,
        })

    # 4. Area Chart
    if (datetime_cols or len(df) > 3) and primary_x and primary_y:
        chart_candidates.append({
            "type": "area",
            "label": "Area Chart",
            "title": f"Cumulative {primary_y} by {primary_x}",
            "x": primary_x,
            "y": primary_y,
            "group": group_col,
        })

    # 5. Donut / Pie Chart (if reasonable category cardinality)
    if object_cols and primary_y and df[primary_x].nunique() <= 16:
        chart_candidates.append({
            "type": "pie",
            "label": "Donut / Pie",
            "title": f"{primary_y} Distribution by {primary_x}",
            "x": primary_x,
            "y": primary_y,
            "group": None,
        })

    # 6. Treemap Chart
    if object_cols and primary_y and len(df) >= 3:
        chart_candidates.append({
            "type": "treemap",
            "label": "Treemap",
            "title": f"Hierarchical Share of {primary_y}",
            "x": primary_x,
            "y": primary_y,
            "group": None,
        })

    # 7. Scatter Plot (if 2+ numeric columns)
    if len(numeric_cols) >= 2:
        chart_candidates.append({
            "type": "scatter",
            "label": "Scatter Correlation",
            "title": f"{numeric_cols[1]} vs {numeric_cols[0]}",
            "x": numeric_cols[0],
            "y": numeric_cols[1],
            "group": object_cols[0] if object_cols else None,
        })

    # Build figures for each candidate
    generated_specs: list[ChartSpec] = []
    seen_types = set()

    # If prompt requested a specific type that's among candidates, prioritize it
    candidate_types = [c["type"] for c in chart_candidates]
    if default_type not in candidate_types and candidate_types:
        default_type = candidate_types[0]

    # Reorder so default_type comes first
    chart_candidates.sort(key=lambda c: 0 if c["type"] == default_type else 1)

    for cand in chart_candidates:
        c_type = cand["type"]
        if c_type in seen_types:
            continue
        seen_types.add(c_type)

        fig_dict = _build_single_figure(
            df=df,
            chart_type=c_type,
            x=cand["x"],
            y=cand["y"],
            group=cand.get("group"),
            title=cand["title"],
        )

        if fig_dict:
            spec: ChartSpec = {
                "chart_type": c_type,
                "label": cand["label"],
                "title": cand["title"],
                "x_axis": cand["x"],
                "y_axis": cand["y"],
                "group_by": cand.get("group"),
                "plotly_figure": fig_dict,
                "is_default": (c_type == default_type),
            }
            generated_specs.append(spec)

    return generated_specs


def visualizer_node(state: AgentState) -> dict[str, Any]:
    """
    Generate multiple chart specifications and Plotly figures based on data and prompt.

    Produces: chart_specs, chart_render_error
    """
    result = state.get("query_result")
    analysis = state.get("analysis")
    user_query = state.get("cleaned_query") or state.get("user_query") or ""

    if not result:
        return {
            "chart_specs": [],
            "chart_render_error": "No data to visualize",
        }

    # Load DataFrame from Parquet
    parquet_path = result.get("parquet_path")
    if not parquet_path:
        return {
            "chart_specs": [],
            "chart_render_error": "No parquet path in query result",
        }

    try:
        df = pd.read_parquet(parquet_path)
    except Exception as e:
        return {
            "chart_specs": [],
            "chart_render_error": f"Failed to read results: {e}",
        }

    suggested_spec = analysis.get("suggested_chart") if analysis else None
    chart_specs = _generate_all_chart_options(
        df=df,
        user_query=user_query,
        suggested_spec=suggested_spec,
    )

    logger.info(
        "Visualization generated %d chart options: %s",
        len(chart_specs),
        [s.get("chart_type") for s in chart_specs],
    )

    return {
        "chart_specs": chart_specs,
        "chart_render_error": None,
    }

