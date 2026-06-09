"""PDF writer for the Analysis-tab comparison report (#12).

Uses reportlab (already a PingPair dep). Layout mirrors the docx
writer: title, runs-included, filter, summary stats, embedded charts,
per-case diff (when applicable).
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..analysis import METRICS, fmt, fmt_delta
from ..analysis.comparison import ComparisonReport
from ._logo import pdf_logo_block
from .run_report import fmt_duration


def _styled_table(headers: list[str], rows: list[list[str]]) -> Table:
    """Build a reportlab Table with the standard report style."""
    data = [headers] + rows
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a3f66")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTSIZE", (0, 1), (-1, -1), 7),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f7f7f9")]),
    ]))
    return table


def write_comparison_pdf(report: ComparisonReport, dest: Path) -> None:
    """Write ``report`` to ``dest`` as a PDF."""
    doc = SimpleDocTemplate(
        str(dest),
        pagesize=landscape(A4),
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title=report.title,
    )
    styles = getSampleStyleSheet()
    body: list = []

    body += pdf_logo_block()  # centred PingPair mark + gap above the title
    body.append(Paragraph(report.title, styles["Title"]))
    body.append(Paragraph(
        f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')} · "
        f"Runs compared: {report.run_count}",
        styles["Normal"],
    ))
    body.append(Spacer(1, 6))

    if report.notes:
        body.append(Paragraph("<b>Notes</b>", styles["Normal"]))
        for line in report.notes.splitlines():
            body.append(Paragraph(line, styles["Normal"]))
        body.append(Spacer(1, 6))

    # Runs included
    body.append(Paragraph("<b>Runs included</b>", styles["Normal"]))
    run_rows: list[list[str]] = []
    for run in report.runs:
        when = (
            run.started_at.strftime("%Y-%m-%d %H:%M")
            if run.started_at is not None
            else "?"
        )
        run_rows.append([
            run.display_label,
            when,
            fmt_duration(run.duration_s),
            "multi" if run.is_multi_segment else "single",
            f"{run.cases_ok}/{run.cases_total} ok",
        ])
    body.append(_styled_table(
        ["Run", "Started", "Duration", "Type", "Cases"],
        run_rows,
    ))
    body.append(Spacer(1, 8))

    # Filter
    if not report.filter_description.is_default:
        body.append(Paragraph("<b>Filter applied</b>", styles["Normal"]))
        bullet_style = ParagraphStyle(
            "FilterBullet", parent=styles["Normal"], leftIndent=18,
        )
        for line in report.filter_description.lines():
            body.append(Paragraph(f"• {line}", bullet_style))
        body.append(Spacer(1, 6))

    # Stats
    body.append(Paragraph("<b>Summary statistics</b>", styles["Normal"]))
    headers = ["Run"]
    for metric in METRICS:
        short = metric.display.split()[0]
        headers.extend([
            f"{short} min", f"{short} avg", f"{short} max",
        ])
    stat_rows: list[list[str]] = []
    for run, rs in zip(report.runs, report.per_run_stats, strict=False):
        row = [run.display_label]
        for metric in METRICS:
            ms = rs.by_metric[metric.code]
            row.extend([fmt(ms.min), fmt(ms.avg), fmt(ms.max)])
        stat_rows.append(row)
    body.append(_styled_table(headers, stat_rows))
    body.append(Spacer(1, 12))

    # Charts
    if report.chart_pngs:
        body.append(PageBreak())
        body.append(Paragraph("<b>Charts</b>", styles["Heading1"]))
        for metric in METRICS:
            png = report.chart_pngs.get(metric.code)
            if png is None or not png.exists():
                continue
            body.append(Paragraph(
                f"<b>{metric.display}</b>", styles["Heading3"]
            ))
            try:
                # Preserve the PNG's aspect ratio (the charts are exported at
                # a fixed ~1.9:1) so they're not stretched into a fixed box —
                # fit within a max width/height so a tall image can't overflow
                # the landscape page frame.
                max_w, max_h = 24 * cm, 14 * cm
                iw, ih = ImageReader(str(png)).getSize()
                if iw and ih:
                    scale = min(max_w / iw, max_h / ih)
                    disp_w, disp_h = iw * scale, ih * scale
                else:
                    disp_w, disp_h = max_w, 12.6 * cm
                img = Image(str(png), width=disp_w, height=disp_h)
                body.append(img)
            except Exception as exc:  # noqa: BLE001
                body.append(Paragraph(
                    f"[chart {metric.display} could not be embedded: {exc}]",
                    styles["Normal"],
                ))
            body.append(Spacer(1, 6))

    # Diff
    if report.has_diff_section:
        body.append(PageBreak())
        a = report.runs[1]
        b = report.runs[0]
        body.append(Paragraph(
            f"<b>Per-case delta — A: {a.display_label}, B: "
            f"{b.display_label}, Δ = B − A</b>",
            styles["Heading2"],
        ))
        diff_headers = ["Case", "Pld", "BW"]
        for metric in METRICS:
            short = metric.display.split()[0]
            diff_headers.extend([
                f"A {short}", f"B {short}", f"Δ {short}",
            ])
        diff_rows: list[list[str]] = []
        for row in report.per_case_diff_rows:
            r = [
                str(row.case_idx),
                str(row.payload_bytes),
                str(row.bandwidth_mbps_pushed),
            ]
            for metric in METRICS:
                r.extend([
                    fmt(row.a_value[metric.code]),
                    fmt(row.b_value[metric.code]),
                    fmt_delta(row.delta[metric.code]),
                ])
            diff_rows.append(r)
        body.append(_styled_table(diff_headers, diff_rows))

    doc.build(body)
