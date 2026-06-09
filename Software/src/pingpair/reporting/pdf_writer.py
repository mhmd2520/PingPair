"""Render a :class:`RunReport` as a PDF via reportlab.

Layout mirrors the docx writer: title block, performance metrics table
(Table-1.PNG shape), per-case detail.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ._logo import pdf_logo_block
from .run_report import (
    CaseMetrics,
    MultiSegmentRunReport,
    RunReport,
    SEGMENT_SUMMARY_HEADERS,
    cross_segment_comparison,
    fmt_duration,
    metadata_rows,
    segment_summary_rows,
)


_HEADERS_MAIN: tuple[str, ...] = (
    "Payload\n(Bytes)",
    "Bandwidth\nPushed (Mbps)",
    "Throughput\nReceived (Mbps)",
    "Jitter\n(ms)",
    "Packet Loss\n(%)",
    "Min Latency\n(ms)",
    "Avg Latency\n(ms)",
    "Max Latency\n(ms)",
)


def _fmt(v: float | None, spec: str) -> str:
    return "—" if v is None else format(v, spec)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=18, leading=22, alignment=1,
        ),
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontSize=14, spaceBefore=12, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=12, spaceBefore=8, spaceAfter=4),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontSize=9, leading=12),
        "footer": ParagraphStyle(
            "footer", parent=base["BodyText"], fontSize=8, textColor=colors.grey, alignment=1,
        ),
    }


def _info_table(report: RunReport) -> Table:
    data = [
        ["Run ID", report.run_id],
        ["Started", report.started_at.strftime("%Y-%m-%d %H:%M:%S")],
        ["Finished", report.ended_at.strftime("%Y-%m-%d %H:%M:%S")],
        ["Duration", fmt_duration(report.duration_s)],
        ["Server IP", report.server_ip],
        ["Client IP", report.client_ip],
        ["Protocol", report.protocol.upper()],
        ["Cases ok", f"{report.cases_ok} / {report.cases_total}"],
        ["fping", report.fping_version],
        ["iperf3", report.iperf3_version],
        ["PingPair", report.app_version],
    ]
    # Append populated metadata rows so the cover page shows technician,
    # customer, hardware S/N etc. in the same shape as the rest.
    for label, value in metadata_rows(report):
        data.append([label, value])
    t = Table(data, colWidths=[40 * mm, 100 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8eef7")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _metrics_table(report: RunReport) -> Table:
    data: list[list[str]] = [list(_HEADERS_MAIN)]
    for c in report.cases:
        data.append([
            str(c.payload_bytes),
            str(c.bandwidth_mbps_pushed),
            _fmt(c.throughput_mbps_received, ".2f"),
            _fmt(c.jitter_ms, ".3f"),
            _fmt(c.packet_loss_pct, ".3f"),
            _fmt(c.min_latency_ms, ".2f"),
            _fmt(c.avg_latency_ms, ".2f"),
            _fmt(c.max_latency_ms, ".2f"),
        ])
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#305496")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f7fb")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _case_detail(c: CaseMetrics, styles: dict[str, ParagraphStyle]) -> list:
    body = styles["body"]
    head = styles["h2"]
    rows = [
        ["Status", c.status],
        ["Error", c.error or "—"],
        ["iperf3 client rc", str(c.iperf3_client_rc) if c.iperf3_client_rc is not None else "—"],
        ["iperf3 server rc", str(c.iperf3_server_rc) if c.iperf3_server_rc is not None else "—"],
        ["fping rc", str(c.fping_rc) if c.fping_rc is not None else "—"],
        ["Throughput", _fmt(c.throughput_mbps_received, ".3f") + " Mbps"],
        ["Jitter", _fmt(c.jitter_ms, ".3f") + " ms"],
        ["Loss", _fmt(c.packet_loss_pct, ".3f") + " %"],
        ["Min / Avg / Max latency",
         f"{_fmt(c.min_latency_ms, '.2f')} / "
         f"{_fmt(c.avg_latency_ms, '.2f')} / "
         f"{_fmt(c.max_latency_ms, '.2f')} ms"],
        ["iperf3 client cmd", c.iperf3_client_cmd],
        ["iperf3 server cmd", c.iperf3_server_cmd],
        ["fping cmd", c.fping_cmd],
    ]
    # Wrap long values in Paragraphs so reportlab line-wraps them.
    wrapped = [
        [Paragraph(label, body), Paragraph(str(value), body)]
        for label, value in rows
    ]
    t = Table(wrapped, colWidths=[40 * mm, 200 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8eef7")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    title = Paragraph(
        f"Case #{c.case_idx:02d} — payload {c.payload_bytes} B, "
        f"bandwidth {c.bandwidth_mbps_pushed} Mbps",
        head,
    )
    return [KeepTogether([title, t, Spacer(1, 4 * mm)])]


def write_pdf(
    report: RunReport,
    dest: Path,
    *,
    include_appendix: bool = False,
) -> None:
    styles = _styles()
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(dest),
        pagesize=landscape(A4),
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=report.run_id,
    )

    story: list = [
        Paragraph("PingPair — LAN Characterization Report", styles["title"]),
        Spacer(1, 6 * mm),
        _info_table(report),
        Spacer(1, 6 * mm),
        Paragraph("Performance Metrics", styles["h1"]),
        Paragraph(
            "Steady-state network with no external simulated or video traffic. "
            "QoS: COS set to 0.",
            styles["body"],
        ),
        Spacer(1, 3 * mm),
        _metrics_table(report),
        PageBreak(),
        Paragraph("Per-case detail", styles["h1"]),
    ]
    story[:0] = pdf_logo_block()  # centred PingPair mark + gap above the title
    for c in report.cases:
        story.extend(_case_detail(c, styles))

    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(f"Generated by PingPair {report.app_version}", styles["footer"])
    )

    if include_appendix:
        import tempfile
        from pathlib import Path as _Path
        from .appendix import append_to_pdf
        with tempfile.TemporaryDirectory(prefix="pingtool_appendix_") as tmp:
            append_to_pdf(story, report, is_multi=False, tmp_dir=_Path(tmp))
            doc.build(story)
    else:
        doc.build(story)


# ---------------------------------------------------------------------------
# Group C-1: multi-segment PDF writer
# ---------------------------------------------------------------------------


def _multi_info_table(report: MultiSegmentRunReport) -> Table:
    data = [
        ["Run ID", report.run_id],
        ["Started", report.started_at.strftime("%Y-%m-%d %H:%M:%S")],
        ["Finished", report.ended_at.strftime("%Y-%m-%d %H:%M:%S")],
        ["Total duration", fmt_duration(report.duration_s)],
        ["Server IP", report.server_ip],
        ["Client IP", report.client_ip],
        ["Protocol", report.protocol.upper()],
        ["Segments ok", f"{report.segments_ok} / {report.segments_total}"],
        ["Total cases ok", f"{report.total_cases_ok} / {report.total_cases}"],
        ["fping", report.fping_version],
        ["iperf3", report.iperf3_version],
        ["PingPair", report.app_version],
    ]
    for label, value in metadata_rows(report):
        data.append([label, value])
    t = Table(data, colWidths=[45 * mm, 195 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8eef7")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _segments_summary_table(report: MultiSegmentRunReport) -> Table:
    data: list[list[str]] = [list(SEGMENT_SUMMARY_HEADERS)]
    for row_values in segment_summary_rows(report):
        data.append(list(row_values))
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#305496")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f7fb")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    return t


def _segment_metrics_table(cases: list[CaseMetrics]) -> Table:
    data: list[list[str]] = [list(_HEADERS_MAIN)]
    for c in cases:
        data.append([
            str(c.payload_bytes),
            str(c.bandwidth_mbps_pushed),
            _fmt(c.throughput_mbps_received, ".2f"),
            _fmt(c.jitter_ms, ".3f"),
            _fmt(c.packet_loss_pct, ".3f"),
            _fmt(c.min_latency_ms, ".2f"),
            _fmt(c.avg_latency_ms, ".2f"),
            _fmt(c.max_latency_ms, ".2f"),
        ])
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#305496")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f7fb")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    return t


def _comparison_table(headers: list[str], rows: list[tuple[str, ...]]) -> Table:
    data: list[list[str]] = [headers] + [list(r) for r in rows]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#305496")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f7fb")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    return t


def write_multi_pdf(
    report: MultiSegmentRunReport,
    dest: Path,
    *,
    include_appendix: bool = False,
) -> None:
    """Multi-segment PDF. Per-case forensic dump skipped on purpose —
    forensics live in the .json sidecar."""
    styles = _styles()
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(dest),
        pagesize=landscape(A4),
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=report.run_id,
    )

    story: list = [
        Paragraph(
            "PingPair — LAN Characterization Report (Multi-Segment)",
            styles["title"],
        ),
        Spacer(1, 6 * mm),
        _multi_info_table(report),
        Spacer(1, 6 * mm),
        Paragraph("Segments summary", styles["h1"]),
        _segments_summary_table(report),
        PageBreak(),
        Paragraph("Per-segment performance metrics", styles["h1"]),
    ]
    story[:0] = pdf_logo_block()  # centred PingPair mark + gap above the title
    for seg in report.segments:
        story.append(Paragraph(
            f"Segment #{seg.segment_idx} — {seg.label} "
            f"({fmt_duration(seg.duration_s)}, {seg.status.upper()})",
            styles["h2"],
        ))
        if seg.error:
            story.append(Paragraph(f"<i>Note: {seg.error}</i>", styles["body"]))
        story.append(_segment_metrics_table(seg.cases))
        story.append(Spacer(1, 4 * mm))

    story.append(PageBreak())
    story.append(Paragraph("Cross-segment comparison", styles["h1"]))
    for metric_id, metric_label in (
        ("throughput", "Throughput Received (Mbps)"),
        ("avg_latency", "Average Latency (ms)"),
        ("loss", "Packet Loss (%)"),
    ):
        story.append(Paragraph(metric_label, styles["h2"]))
        headers, rows = cross_segment_comparison(report, metric=metric_id)
        story.append(_comparison_table(headers, rows))
        story.append(Spacer(1, 4 * mm))

    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(f"Generated by PingPair {report.app_version}", styles["footer"])
    )

    if include_appendix:
        import tempfile
        from pathlib import Path as _Path
        from .appendix import append_to_pdf
        with tempfile.TemporaryDirectory(prefix="pingtool_appendix_") as tmp:
            append_to_pdf(story, report, is_multi=True, tmp_dir=_Path(tmp))
            doc.build(story)
    else:
        doc.build(story)
