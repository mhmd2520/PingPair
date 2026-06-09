"""Headless matplotlib renderer for per-metric charts in reports.

Used by the auto-bundle Analysis appendix (#13 follow-up) so the
docx and pdf reports get embedded line charts — not just tables.
xlsx keeps its native openpyxl LineChart (interactive in Excel),
and txt gets ASCII spark-bars from a different helper.

Pure render → PNG path; the writer modules embed the PNG. We use
matplotlib's Agg backend so this works in a headless Windows
service / CI / PyInstaller bundle without a display server.
"""

from __future__ import annotations

import math
from pathlib import Path

try:
    import matplotlib

    matplotlib.use("Agg")  # headless backend; must precede pyplot import
    import matplotlib.pyplot as plt
    _MATPLOTLIB_AVAILABLE = True
except ImportError:
    # matplotlib is an optional runtime dep — if it's missing, the
    # appendix still renders (stats tables, ASCII sparklines, native
    # xlsx charts) but the docx + pdf chart embeds get skipped
    # silently. Install with ``pip install matplotlib`` to re-enable.
    matplotlib = None  # type: ignore[assignment]
    plt = None  # type: ignore[assignment]
    _MATPLOTLIB_AVAILABLE = False

from .sidecar_loader import LoadedRun  # noqa: E402
from .stats import MetricDef  # noqa: E402


def matplotlib_available() -> bool:
    """Whether the matplotlib chart helpers are usable.

    Callers can branch on this to print a friendlier "install matplotlib"
    hint instead of silently producing a chart-less report.
    """
    return _MATPLOTLIB_AVAILABLE


# Palette aligned with the Analysis tab so charts in saved reports
# match what the user sees on screen when they last looked at the run.
_LINE_COLOUR = "#42a5f5"
_GRID_COLOUR = "#cccccc"
_BG_COLOUR = "#ffffff"


def _flatten_cases(run: LoadedRun) -> list:
    """Flatten every series' cases into one chronological list."""
    out = []
    for series in run.series:
        out.extend(series.cases)
    return out


def render_metric_chart(
    run: LoadedRun,
    metric: MetricDef,
    out_path: Path,
    *,
    width_in: float = 7.0,
    height_in: float = 3.2,
    dpi: int = 130,
) -> Path | None:
    """Render one per-case line chart for ``metric`` to ``out_path``.

    Returns the file path on success, ``None`` if the run has no
    plottable samples for that metric (caller can decide whether to
    skip the figure or substitute placeholder text).
    """
    cases = _flatten_cases(run)
    xs: list[int] = []
    ys: list[float] = []
    for case in cases:
        v = getattr(case, metric.attr)
        if v is None:
            continue
        xs.append(case.case_idx)
        ys.append(float(v))
    if not xs:
        return None
    if not _MATPLOTLIB_AVAILABLE:
        return None

    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)
    fig.patch.set_facecolor(_BG_COLOUR)
    ax.set_facecolor(_BG_COLOUR)

    ax.plot(xs, ys, marker="o", color=_LINE_COLOUR, linewidth=1.8,
            markersize=5, markeredgecolor=_LINE_COLOUR,
            markerfacecolor=_LINE_COLOUR)
    ax.set_xlabel("Case #")
    ax.set_ylabel(metric.y_label)
    ax.set_title(f"{metric.display} per case")
    ax.grid(True, color=_GRID_COLOUR, linewidth=0.5, alpha=0.7)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def render_breakdown_chart(
    run: LoadedRun,
    metric: MetricDef,
    out_path: Path,
    *,
    by: str,
    width_in: float = 7.0,
    height_in: float = 3.2,
    dpi: int = 130,
) -> Path | None:
    """Render an avg-by-axis bar chart (axis = "payload" or "bandwidth").

    Each bar shows the mean of ``metric`` across every filtered case
    sharing that payload size or bandwidth. Useful for spotting "loss
    spikes at 90 Mbps regardless of payload" type patterns at a glance.
    """
    if by not in {"payload", "bandwidth"}:
        raise ValueError(f"unknown breakdown axis: {by}")

    cases = _flatten_cases(run)
    buckets: dict[int, list[float]] = {}
    for case in cases:
        v = getattr(case, metric.attr)
        if v is None:
            continue
        key = (case.payload_bytes if by == "payload"
               else case.bandwidth_mbps_pushed)
        buckets.setdefault(key, []).append(float(v))
    if not buckets:
        return None
    if not _MATPLOTLIB_AVAILABLE:
        return None

    keys = sorted(buckets)
    means = [sum(buckets[k]) / len(buckets[k]) for k in keys]
    labels = [f"{k} {('B' if by == 'payload' else 'Mbps')}" for k in keys]

    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)
    fig.patch.set_facecolor(_BG_COLOUR)
    ax.set_facecolor(_BG_COLOUR)
    ax.bar(labels, means, color=_LINE_COLOUR, edgecolor="#1976d2")
    ax.set_ylabel(f"Avg {metric.display.lower()} ({metric.unit})")
    ax.set_title(
        f"Mean {metric.display.lower()} by "
        f"{'payload size' if by == 'payload' else 'bandwidth'}"
    )
    ax.grid(True, axis="y", color=_GRID_COLOUR, linewidth=0.5, alpha=0.7)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def ascii_sparkline(values: list[float | None]) -> str:
    """Render a list of values as an ASCII bar string for the txt appendix.

    Values mapped to one of `█▇▆▅▄▃▂▁` blocks; ``None`` becomes a space.
    Output length always equals ``len(values)`` — one block per sample.
    """
    blocks = " ▁▂▃▄▅▆▇█"
    # Treat non-finite (nan/inf, e.g. round-tripped from an old 100%-loss
    # sidecar) the same as None — a gap. Without this guard a NaN would reach
    # ``int(round(nan))`` below and raise ValueError, crashing the txt
    # appendix. (Completes the 2026-06-04 NaN-rendering fix.)
    cleaned = [v for v in values if v is not None and math.isfinite(v)]
    if not cleaned:
        return " " * len(values)
    lo, hi = min(cleaned), max(cleaned)
    span = hi - lo or 1
    out: list[str] = []
    for v in values:
        if v is None or not math.isfinite(v):
            out.append(" ")
        else:
            idx = int(round((v - lo) / span * (len(blocks) - 1)))
            out.append(blocks[max(0, min(len(blocks) - 1, idx))])
    return "".join(out)
