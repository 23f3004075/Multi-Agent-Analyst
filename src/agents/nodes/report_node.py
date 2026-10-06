from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.agents.state import AgentState
from src.config import get_settings

logger = logging.getLogger(__name__)


def _safe_url_fetcher(url: str, timeout: int = 10) -> dict:
    if url.startswith("data:"):
        from weasyprint import default_url_fetcher
        return default_url_fetcher(url, timeout)

    raise ValueError(
        f"Blocked URL fetch: {url}. "
        f"Only data: URIs are allowed for security."
    )


def _generate_pdf_reportlab(
    result: dict,
    analysis: dict | None,
    chart_specs: list,
    query: str,
    sql: str,
    output_dir: Path,
) -> str | None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import (
            HRFlowable,
            Image as RLImage,
            KeepTogether,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_path = output_dir / f"report_{timestamp_str}.pdf"

        doc = SimpleDocTemplate(
            str(pdf_path),
            pagesize=letter,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=36,
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=colors.HexColor("#16213e"),
        )
        h2_style = ParagraphStyle(
            "SectionH2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            textColor=colors.HexColor("#0f3460"),
            spaceBefore=12,
            spaceAfter=6,
        )
        body_style = ParagraphStyle(
            "DocBody",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#333333"),
        )
        kpi_style = ParagraphStyle(
            "DocKpi",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#1a1a2e"),
        )
        meta_style = ParagraphStyle(
            "DocMeta",
            parent=styles["Italic"],
            fontName="Helvetica-Oblique",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#666666"),
        )
        code_style = ParagraphStyle(
            "SqlCode",
            parent=styles["Code"],
            fontName="Courier",
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#1a1a2e"),
            backColor=colors.HexColor("#f4f5f8"),
            borderPadding=8,
            borderRadius=4,
        )

        story = []

        story.append(Paragraph("Enterprise SQL Agent — Executive Report", title_style))
        story.append(Spacer(1, 4))
        
        row_count = result.get("row_count", 0)
        truncated_text = " [Results Truncated]" if result.get("truncated") else ""
        gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        story.append(
            Paragraph(
                f"Generated: {gen_time} &bull; Total Rows: {row_count:,}{truncated_text} &bull; Engine: Sandboxed DuckDB",
                meta_style,
            )
        )
        story.append(Spacer(1, 8))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#0f3460"), spaceAfter=10))

        story.append(Paragraph("Analytical Question", h2_style))
        story.append(Paragraph(f"<b>Query:</b> {query}", body_style))
        story.append(Spacer(1, 8))

        if analysis and analysis.get("summary"):
            story.append(Paragraph("Executive Summary", h2_style))
            story.append(Paragraph(analysis.get("summary", ""), kpi_style))
            story.append(Spacer(1, 8))

        if analysis and analysis.get("key_findings"):
            story.append(Paragraph("Key Findings", h2_style))
            for finding in analysis.get("key_findings", []):
                story.append(Paragraph(f"&bull; {finding}", body_style))
                story.append(Spacer(1, 3))
            story.append(Spacer(1, 6))

        if analysis and analysis.get("anomalies"):
            story.append(Paragraph("Anomalies & Notable Outliers", h2_style))
            for anomaly in analysis.get("anomalies", []):
                story.append(Paragraph(f"&bull; {anomaly}", body_style))
                story.append(Spacer(1, 3))
            story.append(Spacer(1, 6))

        if chart_specs and len(chart_specs) > 0:
            fig_dict = chart_specs[0].get("plotly_figure")
            if fig_dict:
                try:
                    import plotly.graph_objects as go
                    fig = go.Figure(fig_dict)
                    fig.update_layout(width=520, height=260, margin=dict(l=30, r=30, t=30, b=30))
                    img_bytes = fig.to_image(format="png", width=520, height=260, scale=1.5)
                    import io
                    img_stream = io.BytesIO(img_bytes)
                    story.append(Paragraph("Data Visualization", h2_style))
                    story.append(RLImage(img_stream, width=500, height=250))
                    story.append(Spacer(1, 10))
                except Exception as chart_err:
                    logger.debug("Chart image export skipped for PDF: %s", chart_err)

        sample_rows = result.get("sample_rows", [])
        if sample_rows:
            story.append(Paragraph(f"Data Preview (Showing first {min(len(sample_rows), 15)} rows)", h2_style))
            headers = list(sample_rows[0].keys())
            table_data = [[Paragraph(f"<b>{col}</b>", meta_style) for col in headers]]
            for row in sample_rows[:15]:
                formatted_row = []
                for col in headers:
                    val = row.get(col, "")
                    str_val = f"{val:,.2f}" if isinstance(val, (int, float)) and not isinstance(val, bool) else str(val)
                    if len(str_val) > 30:
                        str_val = str_val[:27] + "..."
                    formatted_row.append(Paragraph(str_val, meta_style))
                table_data.append(formatted_row)

            col_width = max(35, min(140, 540 // len(headers)))
            col_widths = [col_width] * len(headers)

            t = Table(table_data, colWidths=col_widths, repeatRows=1)
            t.setStyle(
                TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16213e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#ffffff"), colors.HexColor("#f8f9fa")]),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
                ])
            )
            story.append(t)
            story.append(Spacer(1, 12))

        if sql:
            story.append(Paragraph("Generated SQL Query", h2_style))
            escaped_sql = sql.replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(escaped_sql, code_style))
            story.append(Spacer(1, 10))

        story.append(Spacer(1, 15))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc"), spaceAfter=6))
        story.append(
            Paragraph(
                "Enterprise SQL Agent &bull; Autonomous Analytics & Sandboxed Execution",
                meta_style,
            )
        )

        doc.build(story)
        logger.info("ReportLab PDF generated successfully: %s", pdf_path)
        return str(pdf_path)

    except Exception as e:
        logger.exception("ReportLab PDF generation failed: %s", e)
        return None


def _generate_pdf(
    result: dict,
    analysis: dict | None,
    chart_specs: list,
    query: str,
    sql: str,
    output_dir: Path,
) -> str | None:
    pdf_path = _generate_pdf_reportlab(
        result=result,
        analysis=analysis,
        chart_specs=chart_specs,
        query=query,
        sql=sql,
        output_dir=output_dir,
    )
    if pdf_path:
        return pdf_path

    try:
        from jinja2 import BaseLoader, Environment
        from weasyprint import HTML

        template_str = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body { font-family: 'Segoe UI', sans-serif; margin: 30px; color: #1a1a2e; }
                h1 { color: #16213e; border-bottom: 2px solid #0f3460; padding-bottom: 8px; }
            </style>
        </head>
        <body>
            <h1>Analytical Report</h1>
            <p><strong>Query:</strong> {{ query }}</p>
            <div>{{ analysis.summary }}</div>
            <pre>{{ sql }}</pre>
        </body>
        </html>
        """
        env = Environment(loader=BaseLoader(), autoescape=True)
        template = env.from_string(template_str)
        html_content = template.render(query=query, sql=sql, analysis=analysis or {})
        pdf_fallback = output_dir / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        HTML(string=html_content).write_pdf(str(pdf_fallback), url_fetcher=_safe_url_fetcher)
        return str(pdf_fallback)
    except Exception as e:
        logger.warning("WeasyPrint fallback failed: %s", e)
        return None


def _generate_excel(
    result: dict,
    analysis: dict | None,
    query: str,
    sql: str,
    output_dir: Path,
) -> str | None:
    try:
        import xlsxwriter

        output_dir.mkdir(parents=True, exist_ok=True)
        excel_path = output_dir / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        workbook = xlsxwriter.Workbook(
            str(excel_path),
            {"strings_to_formulas": False},
        )

        header_fmt = workbook.add_format({
            "bold": True,
            "bg_color": "#16213e",
            "font_color": "#ffffff",
            "border": 1,
            "font_size": 11,
        })
        cell_fmt = workbook.add_format({
            "border": 1,
            "font_size": 10,
        })
        number_fmt = workbook.add_format({
            "border": 1,
            "font_size": 10,
            "num_format": "#,##0.00",
        })
        title_fmt = workbook.add_format({
            "bold": True,
            "font_size": 14,
            "font_color": "#16213e",
        })
        summary_fmt = workbook.add_format({
            "text_wrap": True,
            "font_size": 10,
        })

        ws1 = workbook.add_worksheet("Executive Dashboard")
        ws1.set_column("A:A", 25)
        ws1.set_column("B:B", 60)

        row = 0
        ws1.write(row, 0, "Analytical Report", title_fmt)
        row += 2
        ws1.write(row, 0, "Query:", header_fmt)
        ws1.write(row, 1, query, summary_fmt)
        row += 1
        ws1.write(row, 0, "Generated:", header_fmt)
        ws1.write(row, 1, datetime.now().strftime("%Y-%m-%d %H:%M"), cell_fmt)
        row += 1
        ws1.write(row, 0, "Rows:", header_fmt)
        ws1.write(row, 1, result.get("row_count", 0), cell_fmt)

        if analysis:
            row += 2
            ws1.write(row, 0, "Summary", title_fmt)
            row += 1
            ws1.write(row, 0, analysis.get("summary", ""), summary_fmt)
            ws1.set_row(row, 45)

            findings = analysis.get("key_findings", [])
            if findings:
                row += 2
                ws1.write(row, 0, "Key Findings", title_fmt)
                for finding in findings:
                    row += 1
                    ws1.write(row, 0, f"• {finding}", summary_fmt)

        parquet_path = result.get("parquet_path")
        if parquet_path:
            try:
                df = pd.read_parquet(parquet_path)
                ws2 = workbook.add_worksheet("Raw Data")

                for col_idx, col_name in enumerate(df.columns):
                    ws2.write(0, col_idx, str(col_name), header_fmt)
                    ws2.set_column(col_idx, col_idx, max(len(str(col_name)) + 2, 12))

                for row_idx, row_data in enumerate(df.itertuples(index=False), 1):
                    for col_idx, value in enumerate(row_data):
                        if isinstance(value, (int, float)):
                            ws2.write_number(row_idx, col_idx, value, number_fmt)
                        else:
                            ws2.write_string(row_idx, col_idx, str(value), cell_fmt)

            except Exception as e:
                logger.warning("Failed to write data tab: %s", e)

        ws3 = workbook.add_worksheet("SQL Query")
        ws3.set_column("A:A", 80)
        ws3.write(0, 0, "SQL Query", title_fmt)
        ws3.write(1, 0, sql, summary_fmt)

        workbook.close()
        logger.info("Excel report generated: %s", excel_path)
        return str(excel_path)

    except Exception as e:
        logger.warning("Excel generation failed: %s", e)
        return None


def report_node(state: AgentState) -> dict[str, Any]:
    result = state.get("query_result")
    analysis = state.get("analysis")
    query = state.get("cleaned_query", "")
    sql = state.get("generated_sql", "")
    chart_specs = state.get("chart_specs", [])

    settings = get_settings()
    output_dir = settings.report_output_dir
    report_paths: dict[str, str] = {}
    errors: list[str] = []

    if not result:
        return {
            "report_paths": {},
            "report_error": "No data for report generation",
        }

    excel_path = _generate_excel(result, analysis, query, sql, output_dir)
    if excel_path:
        report_paths["excel"] = excel_path
    else:
        errors.append("Excel generation failed")

    pdf_path = _generate_pdf(result, analysis, chart_specs, query, sql, output_dir)
    if pdf_path:
        report_paths["pdf"] = pdf_path
    else:
        errors.append("PDF generation failed")

    error_msg = "; ".join(errors) if errors else None

    logger.info("Reports generated: %s", list(report_paths.keys()))

    return {
        "report_paths": report_paths,
        "report_error": error_msg,
    }
