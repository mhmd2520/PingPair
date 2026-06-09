"""Stats sub-tab for the Analysis tab (task #9).

Renders a wide table — one row per ticked run-series — with the
per-metric min / avg / median / max / stdev plus cases-ok counter.
The aggregator lives in :mod:`pingpair.analysis.stats` so this
file is pure UI glue.

Why a separate module: the parent ``_analysis_charts`` widget was
already pushing the cowork file-sync size cap before adding stats
rendering on top. Splitting keeps every file comfortably under the
threshold and isolates the table-rendering code from chart code.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..analysis import (
    METRICS,
    CasePoint,
    LoadedRun,
    fmt,
    run_stats,
)


# Column order matches what a user would scan left-to-right:
# run label · cases-ok · then for each metric: min / avg / max.
# Median and stdev are accessible as tooltips on the avg cell.
_HEADERS: tuple[str, ...] = (
    "Run / segment",
    "Cases ok",
    "Thr min (Mbps)", "Thr avg (Mbps)", "Thr max (Mbps)",
    "Lat min (ms)",   "Lat avg (ms)",   "Lat max (ms)",
    "Loss min (%)",   "Loss avg (%)",   "Loss max (%)",
    "Jit min (ms)",   "Jit avg (ms)",   "Jit max (ms)",
)


class AnalysisStatsPanel(QWidget):
    """One-table summary stats panel for the Analysis tab.

    Constructor takes the same case-filter predicate the chart panel
    uses, so the table refreshes when filters change. ``replot`` is
    the only public entry point — call it with the list of ticked
    (palette_idx, run) tuples whenever upstream state changes.
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(list(_HEADERS))
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        header = self._table.horizontalHeader()
        # First column (label) stretches; numeric columns auto-size to content.
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._table.setFont(mono)
        layout.addWidget(self._table)

    # ------------------------------------------------------------------

    def replot(self, checked: list[tuple[int, LoadedRun]]) -> None:
        """Re-render the table from the current set of ticked runs."""
        # Collect (palette_idx, series_label, series_object, parent_run) so
        # multi-segment runs each get one row labelled
        # ``<run_label> · <segment_label>`` matching the chart legend.
        rows: list[tuple[int, str, LoadedRun]] = []
        for palette_idx, run in checked:
            if run.is_multi_segment:
                # For multi-segment runs we still aggregate the whole run as
                # one row (the per-segment breakdown is in the comparison
                # report on demand, not here — keeps the Stats table from
                # blowing up to 60+ rows on a 3×20-segment overlay).
                rows.append((palette_idx, run.display_label, run))
            else:
                rows.append((palette_idx, run.display_label, run))

        self._table.setRowCount(len(rows))
        for row_idx, (palette_idx, label, run) in enumerate(rows):
            rs = run_stats(run, case_filter=self._case_filter)
            colour = QColor(
                self._palette[palette_idx % len(self._palette)]
            )

            # Column 0 — coloured run label.
            label_item = QTableWidgetItem(label)
            label_item.setForeground(QBrush(colour))
            label_item.setToolTip(str(run.path))
            self._table.setItem(row_idx, 0, label_item)

            # Column 1 — cases ok / total.
            ok_item = QTableWidgetItem(
                f"{rs.filtered_ok}/{rs.filtered_cases}"
            )
            ok_item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            if rs.filtered_cases and rs.filtered_ok < rs.filtered_cases:
                # Highlight any run with a failed case.
                ok_item.setForeground(QBrush(QColor("#ef5350")))
            self._table.setItem(row_idx, 1, ok_item)

            # Columns 2..13 — metric min/avg/max triples in registry order.
            col = 2
            for metric in METRICS:
                ms = rs.by_metric[metric.code]
                for value in (ms.min, ms.avg, ms.max):
                    item = QTableWidgetItem(fmt(value))
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                    self._table.setItem(row_idx, col, item)
                    col += 1
                # Tooltip on the avg column (col-2) carries median + stdev.
                avg_col = col - 2
                tip_lines = [
                    f"samples: {ms.samples}",
                    f"median: {fmt(ms.median)} {metric.unit}",
                    f"stdev:  {fmt(ms.stdev)} {metric.unit}",
                ]
                self._table.item(row_idx, avg_col).setToolTip(
                    "\n".join(tip_lines)
                )

        self._table.resizeColumnsToContents()
        # Re-stretch column 0 after resizeColumnsToContents() collapses it.
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
