"""TXT writer for the Analysis-tab comparison report (#12).

Plain UTF-8 text, no external deps. The intent is "open it in Notepad
and the comparison is legible without scrolling sideways" — so column
widths are computed dynamically from the actual cell contents rather
than guessed.
"""

from __future__ import annotations

from pathlib import Path

from ..analysis import (
    METRICS,
    fmt,
    fmt_delta,
)
from ..analysis.comparison import ComparisonReport
from .run_report import fmt_duration


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Build a simple monospaced text table, returning one string per line."""
    if not headers:
        return []
    widths = [len(h) for h in headers]
    for row in rows:
        for col, cell in enumerate(row):
            if col < len(widths):
                widths[col] = max(widths[col], len(cell))

    def _fmt_row(values: list[str]) -> str:
        return "  ".join(
            v.ljust(widths[i]) for i, v in enumerate(values)
        )

    out = [_fmt_row(headers), _fmt_row(["-" * w for w in widths])]
    for row in rows:
        out.append(_fmt_row(row))
    return out


def write_comparison_txt(report: ComparisonReport, path: Path) -> None:
    """Render ``report`` as a UTF-8 text file at ``path``."""
    lines: list[str] = []
    lines.append(report.title)
    lines.append("=" * len(report.title))
    lines.append("")
    lines.append(
        f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    lines.append(f"Runs compared: {report.run_count}")
    if not report.filter_description.is_default:
        lines.append("Filter applied:")
        for line in report.filter_description.lines():
            lines.append(f"  · {line}")
    if report.notes:
        lines.append("")
        lines.append("Notes:")
        for ln in report.notes.splitlines():
            lines.append(f"  {ln}")
    lines.append("")

    # ---- Runs included ----
    lines.append("Runs included")
    lines.append("-" * len("Runs included"))
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
    lines.extend(_table(
        ["Run", "Started", "Duration", "Type", "Cases"],
        run_rows,
    ))
    lines.append("")

    # ---- Per-run summary stats ----
    lines.append("Summary statistics")
    lines.append("-" * len("Summary statistics"))
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
    lines.extend(_table(headers, stat_rows))
    lines.append("")

    # ---- Diff section (only when len(runs) == 2) ----
    if report.has_diff_section:
        a = report.runs[1]  # older
        b = report.runs[0]  # newer
        lines.append(
            f"Per-case delta — A: {a.display_label}   "
            f"B: {b.display_label}   Δ = B − A"
        )
        lines.append("-" * 60)
        diff_headers = ["Case", "Payload", "BW"]
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
        lines.extend(_table(diff_headers, diff_rows))
        lines.append("")

    # ---- Chart files (referenced by name; the PNGs live next to the txt) ----
    if report.chart_pngs:
        lines.append("Chart files")
        lines.append("-" * len("Chart files"))
        for code, png_path in report.chart_pngs.items():
            lines.append(f"  · {code}: {png_path.name}")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
