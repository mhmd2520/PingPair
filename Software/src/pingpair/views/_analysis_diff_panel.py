"""Diff sub-tab for the Analysis tab (task #11).

When exactly two runs are ticked, this panel renders a per-case table
of B-minus-A deltas across the four metrics. Otherwise it shows a hint
banner telling the user how many runs they need to (un)tick.

The diff math lives in :func:`pingpair.analysis.stats.per_case_diff`
so this widget is pure UI glue. Colouring respects each metric's
``higher_is_better`` flag — a positive throughput delta is green, but
a positive packet-loss delta is red, etc.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..analysis import (
    METRICS,
    CasePoint,
    LoadedRun,
    fmt,
    fmt_delta,
    per_case_diff,
)


# Foreground colour for good / neutral / bad cells. Tuned for the dark
# background — slightly muted from pure green/red so a table full of
# colour doesn't get fatiguing.
_C_GOOD: str = "#66bb6a"
_C_BAD:  str = "#ef5350"
_C_NEUT: str = "#bdbdbd"


def _delta_colour(delta: float | None, higher_is_better: bool) -> str:
    """Map a (delta, direction) pair to one of the three semantic colours."""
    if delta is None:
        return _C_NEUT
    if delta == 0:
        return _C_NEUT
    if (delta > 0) == higher_is_better:
        return _C_GOOD
    return _C_BAD


class AnalysisDiffPanel(QWidget):
    """Per-case delta table — activates when exactly two runs are ticked."""

    def __init__(
        self,
        *,
        case_filter: Callable[[CasePoint], bool],
    ) -> None:
        super().__init__()
        self._case_filter = case_filter

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        # ---- Hint banner ----
        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        outer.addWidget(self._banner)

        # ---- Subtitle (which two runs being compared) ----
        self._subtitle = QLabel("")
        outer.addWidget(self._subtitle)

        # Banner/subtitle colours are theme-driven (were hardcoded dark —
        # near-black box + light-grey text, both low-contrast on the Light
        # theme). Pull from the active palette and re-apply on a theme switch.
        self._apply_theme_styles()

        # ---- Diff table ----
        # Columns: Case # · Payload · Bandwidth · for each metric
        # (A / B / Δ). 3 fixed + 12 metric columns = 15 total.
        headers: list[str] = ["Case", "Payload (B)", "BW (Mbps)"]
        for metric in METRICS:
            short = metric.display.split()[0]  # "Throughput" / "Avg" / "Packet" / "Jitter"
            headers.extend([f"{short} A", f"{short} B", f"Δ {short}"])
        self._headers = tuple(headers)

        self._table = QTableWidget(0, len(self._headers))
        self._table.setHorizontalHeaderLabels(list(self._headers))
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._table.setFont(mono)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        outer.addWidget(self._table, stretch=1)

    # ------------------------------------------------------------------

    def _apply_theme_styles(self) -> None:
        """Colour the banner + subtitle from the active theme palette.

        Decides dark vs. light from the live window lightness (so it follows
        a runtime theme switch), then pulls the curated colours from
        :data:`theme.PALETTES` — the same source the rest of the UI uses.
        """
        is_dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        pal = theme.PALETTES["dark" if is_dark else "light"]
        self._banner.setStyleSheet(
            f"QLabel {{ background: {pal['surface']}; color: {pal['text']}; "
            f"padding: 6px; border-radius: 4px; }}"
        )
        self._subtitle.setStyleSheet(f"color: {pal['subtext']};")

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt override)
        """Re-theme the banner/subtitle when the application palette changes."""
        if event.type() == QEvent.Type.PaletteChange:
            self._apply_theme_styles()
        super().changeEvent(event)

    def replot(self, checked: list[tuple[int, LoadedRun]]) -> None:
        n = len(checked)
        if n != 2:
            if n == 0:
                self._banner.setText(
                    "Tick exactly two runs on the left to see a per-case "
                    "delta table here. (Currently 0 ticked.)"
                )
            elif n == 1:
                self._banner.setText(
                    "Diff view needs two ticked runs. Tick one more on "
                    "the left and the delta table will populate."
                )
            else:
                self._banner.setText(
                    f"Diff view shows pairwise deltas — currently {n} runs "
                    "ticked. Untick all but two to compare."
                )
            self._subtitle.setText("")
            self._table.setRowCount(0)
            return

        (_a_idx, run_a), (_b_idx, run_b) = checked[0], checked[1]
        # The runs list sorts newest-first, so checked[0] is the more recent
        # run. People tend to read "B vs A" as "newer vs older" — flip so
        # the first ticked (newer) plays "B" and the second (older) plays "A".
        run_b, run_a = run_a, run_b

        rows = per_case_diff(run_a, run_b, case_filter=self._case_filter)
        self._banner.setText(
            f"Showing {len(rows)} case row(s). Positive Δ values are "
            "coloured by whether they're an improvement for that metric "
            "(higher throughput = green, higher latency / loss / jitter "
            "= red)."
        )
        self._subtitle.setText(
            f"A (older): {run_a.display_label}    "
            f"B (newer): {run_b.display_label}    "
            f"Δ = B − A"
        )

        self._table.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            self._table.setItem(
                row_idx, 0, _centered(str(row.case_idx))
            )
            self._table.setItem(
                row_idx, 1, _centered(str(row.payload_bytes))
            )
            self._table.setItem(
                row_idx, 2, _centered(str(row.bandwidth_mbps_pushed))
            )
            col = 3
            for metric in METRICS:
                a_item = _aligned(fmt(row.a_value[metric.code]))
                b_item = _aligned(fmt(row.b_value[metric.code]))
                d_item = _aligned(fmt_delta(row.delta[metric.code]))
                colour = _delta_colour(
                    row.delta[metric.code], metric.higher_is_better
                )
                d_item.setForeground(QBrush(QColor(colour)))
                self._table.setItem(row_idx, col, a_item)
                self._table.setItem(row_idx, col + 1, b_item)
                self._table.setItem(row_idx, col + 2, d_item)
                col += 3

        self._table.resizeColumnsToContents()


def _centered(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return item


def _aligned(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    )
    return item
