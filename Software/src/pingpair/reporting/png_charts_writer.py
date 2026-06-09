"""Standalone PNG chart export (Task N, 2026-05-12).

For every saved sweep, emit an ``Analysis_Images/`` subfolder alongside the
docx/xlsx/pdf/txt files containing one PNG per metric (per-case line
chart) plus per-payload and per-bandwidth breakdown bar charts. The
PNGs are useful for embedding in slide decks or email summaries
without needing to open the Word file. The matplotlib renderer is
the same headless helper the appendix uses.

If matplotlib isn't installed, this writer silently writes a single
``charts_unavailable.txt`` note instead of crashing.
"""

from __future__ import annotations

from pathlib import Path

from ..analysis import METRICS
from ..analysis.chart_renderer import (
    matplotlib_available,
    render_breakdown_chart,
    render_metric_chart,
)
from ..analysis.run_to_loaded import (
    from_multi_segment_report,
    from_run_report,
)


def write_png_charts(report, dest: Path, *, is_multi: bool) -> list[Path]:
    """Write Charts/ PNGs alongside the per-sweep reports.

    ``dest`` is the sweep's folder (e.g. ``Reports/PingPair_<ts>/``).
    An ``Analysis_Images/`` subfolder is created inside; six PNGs are written:

    * ``thr.png`` / ``lat.png`` / ``loss.png`` / ``jit.png`` —
      per-case line chart for each metric.
    * ``by_payload.png`` / ``by_bandwidth.png`` — mean throughput
      bar charts grouped by the test-grid axes.

    Returns the list of files written. Empty list if matplotlib is
    missing.
    """
    charts_dir = dest / "Analysis_Images"
    charts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if not matplotlib_available():
        note = charts_dir / "charts_unavailable.txt"
        note.write_text(
            "matplotlib is not installed in this Python environment, so "
            "the chart PNGs could not be rendered. Install with:\n"
            "    pip install matplotlib\n",
            encoding="utf-8",
        )
        return [note]

    loaded = (from_multi_segment_report(report)
              if is_multi else from_run_report(report))

    # Per-metric line charts (skip metrics with no data).
    for metric in METRICS:
        out = charts_dir / f"{metric.code}.png"
        result = render_metric_chart(loaded, metric, out)
        if result is not None:
            written.append(result)

    # Breakdown bar charts: one per axis, only the throughput metric to
    # keep the count manageable. Latency/loss/jitter breakdowns can be
    # added later if useful.
    bp = charts_dir / "by_payload.png"
    if render_breakdown_chart(loaded, METRICS[0], bp, by="payload") is not None:
        written.append(bp)
    bb = charts_dir / "by_bandwidth.png"
    if render_breakdown_chart(loaded, METRICS[0], bb, by="bandwidth") is not None:
        written.append(bb)

    return written
