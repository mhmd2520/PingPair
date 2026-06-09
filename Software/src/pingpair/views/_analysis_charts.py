"""Chart panel for the Analysis tab.

Wraps a QTabWidget containing four pyqtgraph PlotWidget panes
(throughput / avg latency / packet loss / jitter), plus the Stats
sub-tab (#9), Trend sub-tab (#10), and Diff sub-tab (#11). The Export /
Save-PNG buttons live in the Analysis tab's left pane (under Filters) and
drive :meth:`save_current_png`; ``on_png_state_changed`` tells the view
when to enable the Save-PNG button. Call replot() with the list of ticked
runs whenever the upstream selection or filter changes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..analysis import LoadedRun
from ._analysis_diff_panel import AnalysisDiffPanel
from ._analysis_stats_panel import AnalysisStatsPanel
from ._analysis_trend_panel import AnalysisTrendPanel

PALETTE: tuple[str, ...] = (
    "#42a5f5", "#ef5350", "#66bb6a", "#ffa726", "#ab47bc",
    "#26c6da", "#ec407a", "#9ccc65", "#8d6e63", "#bdbdbd",
)

_METRICS: tuple[tuple[str, str, str], ...] = (
    ("Throughput",   "throughput_mbps_received", "Throughput (Mbps)"),
    ("Avg latency",  "avg_latency_ms",           "Avg latency (ms)"),
    ("Packet loss",  "packet_loss_pct",          "Packet loss (%)"),
    ("Jitter",       "jitter_ms",                "Jitter (ms)"),
)

_SEGMENT_SYMBOLS: tuple[str, ...] = ("o", "s", "t", "d")
_SEGMENT_PEN_STYLES: tuple[Qt.PenStyle, ...] = (
    Qt.PenStyle.SolidLine,
    Qt.PenStyle.DashLine,
    Qt.PenStyle.DotLine,
    Qt.PenStyle.DashDotLine,
)

# Standard rasterise size for every chart PNG (the Save-PNG button and the
# comparison-report exporter both use this so the images match). A fixed,
# WIDE canvas — ~1.9:1, like the on-screen Throughput tab.
CHART_EXPORT_WIDTH_PX = 1600
CHART_EXPORT_HEIGHT_PX = 840


def export_plot_png(plot_item, dest: str | Path) -> None:
    """Rasterise a pyqtgraph ``PlotItem`` to a PNG at the standard size.

    Every metric is exported at the same ``CHART_EXPORT_WIDTH_PX`` ×
    ``CHART_EXPORT_HEIGHT_PX`` canvas. pyqtgraph derives the export height
    from the source view's on-screen scene rect, which differs per tab — the
    *visible* chart tab is wide, but the *hidden* tabs keep pyqtgraph's
    default ~4:3 rect and so exported squished (the jit/lat/loss vs thr
    mismatch). To make all four match we force the backing view to the target
    size first: ``GraphicsView.resizeEvent`` rebuilds the scene rect from
    ``self.size()``. The original size is restored synchronously in
    ``finally`` (before any repaint), so the live chart isn't disturbed.

    Raises ``ImportError`` if ``pyqtgraph.exporters`` is unavailable, or the
    exporter's own error on a write failure — callers own the user messaging.
    """
    from pyqtgraph.exporters import ImageExporter

    target = QSize(CHART_EXPORT_WIDTH_PX, CHART_EXPORT_HEIGHT_PX)
    scene = plot_item.scene()
    views = scene.views() if scene is not None else []
    view = views[0] if views else None
    old = view.size() if view is not None else None
    try:
        if view is not None and old is not None:
            view.resize(target)
            view.resizeEvent(QResizeEvent(target, old))
        exporter = ImageExporter(plot_item)
        exporter.parameters()["width"] = CHART_EXPORT_WIDTH_PX
        exporter.export(str(dest))
    finally:
        if view is not None and old is not None:
            view.resize(old)
            view.resizeEvent(QResizeEvent(old, target))


class AnalysisCharts(QWidget):
    """Right-hand chart panel for the Analysis tab."""

    def __init__(
        self,
        *,
        run_filter: Callable[[LoadedRun], bool],
        case_filter: Callable[[object], bool],
        get_source_dir: Callable[[], Path],
        status_callback: Callable[[str], None],
        on_png_state_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self._run_filter = run_filter
        self._case_filter = case_filter
        self._get_source_dir = get_source_dir
        self._status = status_callback
        # The Export / Save-PNG buttons live in the left pane now (under Filters);
        # this callback lets the view enable/disable its Save-PNG button as the
        # active chart sub-tab changes (only the plot tabs are savable).
        self._on_png_state_changed = on_png_state_changed

        pg.setConfigOption("background", "#1e1e1e")
        pg.setConfigOption("foreground", "#dddddd")
        pg.setConfigOption("antialias", True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._tabs = QTabWidget()
        self._plots: dict[str, pg.PlotWidget] = {}
        self._legends: dict[str, pg.LegendItem] = {}
        for name, _attr, y_label in _METRICS:
            pw = pg.PlotWidget()
            pw.showGrid(x=True, y=True, alpha=0.3)
            pw.setLabel("bottom", "Case #")
            pw.setLabel("left", y_label)
            legend = pw.addLegend(offset=(10, 10))
            self._plots[name] = pw
            self._legends[name] = legend
            self._tabs.addTab(pw, name)
        self._stats_panel = AnalysisStatsPanel(
            case_filter=case_filter, palette=PALETTE,
        )
        self._tabs.addTab(self._stats_panel, "Stats")
        self._trend_panel = AnalysisTrendPanel(
            case_filter=case_filter, palette=PALETTE,
        )
        self._tabs.addTab(self._trend_panel, "Trend")
        self._diff_panel = AnalysisDiffPanel(case_filter=case_filter)
        self._tabs.addTab(self._diff_panel, "Diff")
        self._tabs.currentChanged.connect(self._emit_png_state)
        layout.addWidget(self._tabs, stretch=1)
        self._emit_png_state()

    def metric_plot_widget(self, metric_code: str):
        """Return the :class:`pg.PlotWidget` for one of the four metrics.

        Used by :class:`AnalysisView` to rasterise charts to PNG before
        building the ComparisonReport. ``metric_code`` is one of
        ``"thr"`` / ``"lat"`` / ``"loss"`` / ``"jit"``.
        """
        # Map the stats.MetricCode → the chart panel's tab display name.
        code_to_name = {
            "thr":  "Throughput",
            "lat":  "Avg latency",
            "loss": "Packet loss",
            "jit":  "Jitter",
        }
        name = code_to_name.get(metric_code)
        return self._plots.get(name) if name else None

    def has_savable_chart(self) -> bool:
        """Whether the active sub-tab is a chart that can be saved as PNG."""
        return self._current_plot_widget() is not None

    def _current_plot_widget(self):
        current = self._tabs.currentWidget()
        if isinstance(current, pg.PlotWidget):
            return current
        if current is not None:
            for child in current.findChildren(pg.PlotWidget):
                return child
        return None

    def _emit_png_state(self) -> None:
        if self._on_png_state_changed is not None:
            self._on_png_state_changed(self._current_plot_widget() is not None)

    def save_current_png(self) -> None:
        """Save the currently-visible chart to a PNG (driven by the left-pane
        button). No-op if the active sub-tab isn't a savable chart."""
        current = self._current_plot_widget()
        if current is None:
            return
        tab_idx = self._tabs.currentIndex()
        tab_name = self._tabs.tabText(tab_idx) or "chart"
        safe_name = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in tab_name
        ).strip("_") or "chart"
        default_path = self._get_source_dir() / f"analysis_{safe_name}.png"
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            f"Save {tab_name} chart as PNG",
            str(default_path),
            "PNG image (*.png);;All files (*.*)",
        )
        if not chosen:
            return
        try:
            export_plot_png(current.plotItem, chosen)
        except ImportError as exc:
            QMessageBox.critical(
                self, "PNG export unavailable",
                f"pyqtgraph.exporters is missing: {exc}",
            )
            return
        except Exception as exc:
            QMessageBox.critical(
                self, "PNG export failed",
                f"Could not save PNG:\n\n{exc}",
            )
            return
        self._status(f"Saved {tab_name} chart: {Path(chosen).name}")

    def replot(self, checked: list[tuple[int, LoadedRun]]) -> None:
        filtered = [
            (idx, run) for idx, run in checked if self._run_filter(run)
        ]
        if hasattr(self, "_stats_panel"):
            self._stats_panel.replot(filtered)
        if hasattr(self, "_trend_panel"):
            self._trend_panel.replot(filtered)
        if hasattr(self, "_diff_panel"):
            self._diff_panel.replot(filtered)

        for name, attr, _y_label in _METRICS:
            pw = self._plots[name]
            pw.clear()
            legend = self._legends[name]
            try:
                legend.clear()
            except Exception:
                # Older pyqtgraph LegendItem has no clear() — fall back to
                # detaching the old legend and building a fresh one. This
                # except is the expected version-compat path, not a
                # swallowed error.
                try:
                    pw.scene().removeItem(legend)
                except Exception:
                    # Legend already detached from the scene — harmless,
                    # the addLegend below replaces it regardless.
                    pass
                legend = pw.addLegend(offset=(10, 10))
                self._legends[name] = legend

            for palette_idx, run in checked:
                if not self._run_filter(run):
                    continue
                colour = PALETTE[palette_idx % len(PALETTE)]
                for seg_idx, series in enumerate(run.series):
                    xs: list[int] = []
                    ys: list[float] = []
                    for case in series.cases:
                        if not self._case_filter(case):
                            continue
                        value = getattr(case, attr)
                        if value is None:
                            continue
                        xs.append(case.case_idx)
                        ys.append(float(value))
                    if not xs:
                        continue
                    symbol = _SEGMENT_SYMBOLS[seg_idx % len(_SEGMENT_SYMBOLS)]
                    pen_style = _SEGMENT_PEN_STYLES[
                        seg_idx % len(_SEGMENT_PEN_STYLES)
                    ]
                    pw.plot(
                        xs, ys,
                        pen=pg.mkPen(colour, width=2, style=pen_style),
                        symbol=symbol,
                        symbolSize=6,
                        symbolBrush=colour,
                        symbolPen=colour,
                        name=series.label,
                    )
