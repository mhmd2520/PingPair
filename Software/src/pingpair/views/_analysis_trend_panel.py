"""Trend-over-time sub-tab for the Analysis tab (task #10).

Plots one marker per ticked run, x-axis = ``started_at`` (chronological,
left-to-right oldest-to-newest), y-axis = the chosen aggregate metric
(default: average throughput). A small toolbar dropdown lets the user
flip between the four metrics without leaving the tab.

Multi-segment runs aggregate across every segment — one marker per
*run*, not per segment. The Stats sub-tab keeps the per-segment
detail; this tab is for "are runs drifting over time" at a glance.

Runs without a ``started_at`` timestamp are skipped (the v1 sidecar
era didn't write one); a small footer label tells the user how many
were dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..analysis import (
    METRICS,
    CasePoint,
    LoadedRun,
    MetricCode,
    metric_by_code,
    run_stats,
)


class AnalysisTrendPanel(QWidget):
    """Chronological scatter of one aggregate metric per run.

    Parameters mirror :class:`AnalysisCharts`'s contract — pass in the
    same case-filter predicate and palette so the trend matches the
    Throughput / Avg latency / Packet loss / Jitter charts.
    """

    def __init__(
        self,
        *,
        case_filter: Callable[[CasePoint], bool],
        palette: tuple[str, ...],
    ) -> None:
        super().__init__()
        self._case_filter = case_filter
        self._palette = palette
        self._current_metric: MetricCode = "thr"
        # Last list of checked runs, retained for re-render on metric switch
        # without forcing the parent to re-call replot().
        self._last_checked: list[tuple[int, LoadedRun]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        # ---- Top row: metric picker ----
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Aggregate metric:"))
        self._metric_combo = QComboBox()
        for metric in METRICS:
            self._metric_combo.addItem(metric.display, userData=metric.code)
        self._metric_combo.currentIndexChanged.connect(self._on_metric_changed)
        bar.addWidget(self._metric_combo)
        bar.addStretch(1)
        outer.addLayout(bar)

        # ---- Plot ----
        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setLabel("bottom", "Run start time")
        self._plot.setLabel("left", metric_by_code("thr").y_label)
        # Use DateAxis so the x ticks are wall-clock, not epoch seconds.
        date_axis = pg.DateAxisItem(orientation="bottom")
        self._plot.setAxisItems({"bottom": date_axis})
        outer.addWidget(self._plot, stretch=1)

        # ---- Footer (skipped-runs hint) ----
        self._footer = QLabel("")
        self._footer.setStyleSheet("color:#888;")
        self._footer.setWordWrap(True)
        outer.addWidget(self._footer)

    # ------------------------------------------------------------------

    @Slot()
    def _on_metric_changed(self, _idx: int) -> None:
        code = self._metric_combo.currentData()
        if isinstance(code, str):
            self._current_metric = code  # type: ignore[assignment]
        self._render()

    def replot(self, checked: list[tuple[int, LoadedRun]]) -> None:
        """Persist the ticked-runs list and re-render the plot."""
        self._last_checked = list(checked)
        self._render()

    # ------------------------------------------------------------------

    def _render(self) -> None:
        self._plot.clear()
        # Update y-axis label to match the current metric.
        metric = metric_by_code(self._current_metric)
        self._plot.setLabel("left", metric.y_label)

        skipped_no_ts = 0
        skipped_no_data = 0
        points: list[tuple[datetime, float, int, str]] = []  # ts, val, palette_idx, label

        for palette_idx, run in self._last_checked:
            if run.started_at is None:
                skipped_no_ts += 1
                continue
            rs = run_stats(run, case_filter=self._case_filter)
            value = rs.by_metric[self._current_metric].avg
            if value is None:
                skipped_no_data += 1
                continue
            points.append(
                (run.started_at, value, palette_idx, run.display_label)
            )

        if not points:
            self._footer.setText(
                "No plottable runs — tick at least one run with a known "
                "start time and non-empty metric samples."
            )
            return

        # Sort by timestamp so connecting lines run left-to-right.
        points.sort(key=lambda p: p[0])
        xs = [p[0].timestamp() for p in points]
        ys = [p[1] for p in points]
        # Connect with a thin grey line so drift is visually obvious;
        # marker brushes use each run's palette colour so legend
        # alignment with the four metric charts is preserved.
        self._plot.plot(
            xs, ys,
            pen=pg.mkPen("#888888", width=1, style=Qt.PenStyle.DashLine),
        )
        for x, y, palette_idx, _label in points:
            colour = self._palette[palette_idx % len(self._palette)]
            scatter = pg.ScatterPlotItem(
                x=[x.timestamp() if isinstance(x, datetime) else x],
                y=[y],
                size=10,
                pen=pg.mkPen(colour, width=2),
                brush=pg.mkBrush(colour),
                symbol="o",
            )
            self._plot.addItem(scatter)

        hint_bits: list[str] = []
        if skipped_no_ts:
            hint_bits.append(
                f"{skipped_no_ts} run(s) skipped — missing start time."
            )
        if skipped_no_data:
            hint_bits.append(
                f"{skipped_no_data} run(s) skipped — no {metric.display} "
                "samples after filter."
            )
        self._footer.setText(
            " · ".join(hint_bits) if hint_bits
            else f"Plotting {len(points)} run(s) chronologically."
        )
