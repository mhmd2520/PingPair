"""20-row sweep table — one row per (payload, bandwidth) test case.

Pre-populated with the canonical plan from :func:`core.plan.build_plan` so
the user sees the full sweep ahead of time. Each row's metric columns
are filled in as cases finish; the active row's Status cell is tinted;
failed rows show red.

Group B adds a leftmost **Run** checkbox column. Cases with the box
unchecked are skipped over by ``ControlClient.run_sweep`` and don't
appear in the resulting report. ``selected_case_indexes()`` /
``set_selected_case_indexes()`` persist the user's choice via
``RunState`` and ``QSettings``.

The Run checkbox is a centred :class:`QCheckBox` cell widget (not a
checkable item): the indicator sits in the middle of the cell and a click
anywhere in the cell toggles it. The table itself uses
``NoSelection`` — the checkbox is the single source of "this case will
run", so there's no separate Qt row-highlight to desync from it.

The table is driven from :class:`pingpair.core.control.client.SweepCaseEntry`
records that the SweepWorker emits.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..core.control.client import SweepCaseEntry
from ..core.plan import TestCase, build_plan
from ..core.runner import estimate_case_wall_s
from ..config import AppConfig

# Column 0 is the per-row Run checkbox; the remaining columns mirror the
# Phase 3b layout. Keep ``_HEADERS`` in sync with the constants below if
# any are renumbered.
_COL_RUN: Final[int] = 0
_COL_INDEX: Final[int] = 1
_COL_PAYLOAD: Final[int] = 2
_COL_BW: Final[int] = 3
_COL_STATUS: Final[int] = 4
_COL_THROUGHPUT: Final[int] = 5
_COL_JITTER: Final[int] = 6
_COL_LOSS: Final[int] = 7
_COL_MIN: Final[int] = 8
_COL_AVG: Final[int] = 9
_COL_MAX: Final[int] = 10

_HEADERS: Final[tuple[str, ...]] = (
    "Run",
    "#",
    "Payload (B)",
    "BW (Mbps)",
    "Status",
    "Throughput (Mbps)",
    "Jitter (ms)",
    "Loss (%)",
    "Min (ms)",
    "Avg (ms)",
    "Max (ms)",
)

# Status palette tuned for the dark theme.
_STATUS_PENDING = ("#444444", "Pending")
_STATUS_RUNNING = ("#1565c0", "Running")
_STATUS_OK = ("#1f6f3a", "OK")
_STATUS_ERROR = ("#8b1a1a", "Error")
_STATUS_SKIPPED = ("#2a2a2a", "Skipped")


class _CheckCell(QWidget):
    """Holds a centred :class:`QCheckBox` and toggles it on a cell click.

    Used as the Run-column cell widget so the check indicator sits in the
    *middle* of the cell (a checkable ``QTableWidgetItem`` draws it hard-left
    and ignores alignment) and clicking anywhere in the cell — not just on the
    16 px box — toggles it. Clicks that land on the checkbox itself are
    handled by the checkbox; clicks beside it are forwarded here, so there's
    never a double toggle.
    """

    def __init__(self, checkbox: QCheckBox) -> None:
        super().__init__()
        self._cb = checkbox
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(checkbox)

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        # Click anywhere in the cell (beside the box) toggles it, unless the
        # subset is locked mid-sweep (checkbox disabled).
        if self._cb.isEnabled():
            self._cb.toggle()
        event.accept()


class SweepTable(QTableWidget):
    """20 rows × 11 columns. Pre-filled with case # / payload / bw + Run checkbox."""

    # Emitted whenever the user toggles a Run checkbox. Argument: number
    # of currently-checked rows. The Client panel uses this to update the
    # counter chip and enable/disable the Run button.
    selection_changed = Signal(int)

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(0, len(_HEADERS))
        self.setHorizontalHeaderLabels(_HEADERS)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # No row-selection: the Run checkbox is the single source of "this
        # case will run". A separate Qt row-highlight (which could highlight
        # one row while a different row was checked) only confused users.
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        header = self.horizontalHeader()
        for i in range(len(_HEADERS)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeMode.Stretch)

        self._cfg = cfg
        self._cases: list[TestCase] = []
        self._suppress_signal = False  # set during bulk updates
        # row -> the centred QCheckBox for that row's Run column.
        self._run_checks: dict[int, QCheckBox] = {}
        self.populate(build_plan(cfg))

    # ----- public API --------------------------------------------------

    def populate(self, cases: list[TestCase]) -> None:
        """Wipe the table and pre-fill rows from a fresh plan.

        All rows start with their Run checkbox checked - the same default
        the app has had since Phase 3b. Persisted selection is applied
        afterwards via :meth:`set_selected_case_indexes`.
        """
        self._suppress_signal = True
        try:
            self._cases = list(cases)
            self._run_checks.clear()
            self.setRowCount(len(cases))
            for row, case in enumerate(cases):
                self._ensure_run_checkbox(row, checked=True)
                self._set(row, _COL_INDEX, str(case.index))
                self._set(row, _COL_PAYLOAD, str(case.payload_bytes))
                self._set(row, _COL_BW, str(case.bandwidth_mbps))
                self._set_status(row, *_STATUS_PENDING)
                for col in range(_COL_THROUGHPUT, len(_HEADERS)):
                    self._set(row, col, "—")
        finally:
            self._suppress_signal = False
        self.selection_changed.emit(self.selected_count())

    def mark_running(self, case_idx: int) -> None:
        """Highlight the row for the case that just started."""
        row = self._row_for(case_idx)
        if row is None:
            return
        self._set_status(row, *_STATUS_RUNNING)
        # Auto-scroll so the user sees what's happening on long sweeps.
        self.setCurrentCell(row, _COL_INDEX)
        self.scrollToItem(self.item(row, _COL_INDEX))

    def mark_done(self, entry: SweepCaseEntry) -> None:
        """Fill in the metric columns for a finished case."""
        row = self._row_for(entry.case.index)
        if row is None:
            return

        if entry.ok:
            self._set_status(row, *_STATUS_OK)
        else:
            self._set_status(row, *_STATUS_ERROR)

        cr = entry.case_result
        if cr is not None and cr.iperf_client is not None:
            self._set(row, _COL_THROUGHPUT, f"{cr.iperf_client.throughput_mbps:.2f}")
            self._set(row, _COL_JITTER, f"{cr.iperf_client.jitter_ms:.3f}")
            self._set(row, _COL_LOSS, f"{cr.iperf_client.packet_loss_pct:.3f}")
        if cr is not None and cr.fping is not None:
            self._set(row, _COL_MIN, f"{cr.fping.min_ms:.2f}")
            self._set(row, _COL_AVG, f"{cr.fping.avg_ms:.2f}")
            self._set(row, _COL_MAX, f"{cr.fping.max_ms:.2f}")

        if entry.error:
            tip = entry.error
            for col in range(self.columnCount()):
                cell = self.item(row, col)
                if cell is not None:
                    cell.setToolTip(tip)

    def mark_skipped_unselected(self) -> None:
        """Tint every unchecked row's status cell as 'Skipped'.

        Called by the Client panel just before kicking off a sweep so
        the user can see at a glance which cases the upcoming run will
        leave alone.
        """
        for row in range(self.rowCount()):
            if not self._is_row_checked(row):
                self._set_status(row, *_STATUS_SKIPPED)
            else:
                self._set_status(row, *_STATUS_PENDING)

    def reset(self) -> None:
        """Wipe metric columns and reset all rows to Pending.

        Preserves the current Run-checkbox state so the user doesn't lose
        their subset between consecutive sweeps.
        """
        current = set(self.selected_case_indexes())
        self.populate(self._cases)
        # ``populate`` re-checks every row by default; restore the prior
        # selection if the user had narrowed it.
        if current and len(current) != len(self._cases):
            self.set_selected_case_indexes(sorted(current))

    # ----- selection ---------------------------------------------------

    def selected_case_indexes(self) -> list[int]:
        """Return the 1-based case indexes whose Run checkbox is ticked."""
        out: list[int] = []
        for row, case in enumerate(self._cases):
            if self._is_row_checked(row):
                out.append(case.index)
        return out

    def selected_count(self) -> int:
        return len(self.selected_case_indexes())

    def total_count(self) -> int:
        return len(self._cases)

    def set_selected_case_indexes(self, indexes: list[int]) -> None:
        """Restore the user's prior selection (e.g. from QSettings).

        Empty list means "select all" (matches the persisted-default
        convention - see :class:`RunState.selected_case_indexes`).
        """
        wanted = set(indexes) if indexes else {c.index for c in self._cases}
        self._suppress_signal = True
        try:
            for row, case in enumerate(self._cases):
                self._set_row_checked(row, case.index in wanted)
        finally:
            self._suppress_signal = False
        self.selection_changed.emit(self.selected_count())

    def select_all(self) -> None:
        self.set_selected_case_indexes([c.index for c in self._cases])

    def select_none(self) -> None:
        # Use a sentinel non-empty list - empty would re-trigger
        # "select all" semantics in :meth:`set_selected_case_indexes`.
        self._suppress_signal = True
        try:
            for row in range(self.rowCount()):
                self._set_row_checked(row, False)
        finally:
            self._suppress_signal = False
        self.selection_changed.emit(0)

    def toggle_payload(self, payload_bytes: int) -> None:
        """Toggle every row whose payload matches ``payload_bytes``.

        If any matching row is unchecked, this checks them all; otherwise
        it unchecks them all. Mirrors the way Excel toggles a column.
        """
        rows = [
            row for row, c in enumerate(self._cases)
            if c.payload_bytes == payload_bytes
        ]
        target = not all(self._is_row_checked(r) for r in rows)
        self._bulk_set(rows, target)

    def toggle_bandwidth(self, bandwidth_mbps: int) -> None:
        rows = [
            row for row, c in enumerate(self._cases)
            if c.bandwidth_mbps == bandwidth_mbps
        ]
        target = not all(self._is_row_checked(r) for r in rows)
        self._bulk_set(rows, target)

    def estimated_duration_s(self) -> float:
        """ETA for the currently-checked subset.

        Per-case wall time comes from
        :func:`core.runner.estimate_case_wall_s` — a duration-aware
        model (fping running at the Windows timer granularity is what
        bounds the case). Sum over checked rows only — unchecked cases
        are skipped at run time so they don't contribute.
        """
        interval_ms = float(self._cfg.fping.interval_ms)
        return sum(
            estimate_case_wall_s(c.duration_s, interval_ms)
            for row, c in enumerate(self._cases)
            if self._is_row_checked(row)
        )

    def set_interactive(self, interactive: bool) -> None:
        """Lock or unlock the Run checkboxes.

        Called by ``_ClientPanel`` while a sweep is in flight so the user
        can't change the subset mid-run. Only the checkboxes are toggled -
        the rest of the table stays scrollable.
        """
        for cb in self._run_checks.values():
            cb.setEnabled(interactive)

    # ----- internals ---------------------------------------------------

    def _row_for(self, case_idx: int) -> int | None:
        for i, case in enumerate(self._cases):
            if case.index == case_idx:
                return i
        return None

    def _ensure_run_checkbox(self, row: int, *, checked: bool) -> None:
        """Create (or replace) the centred Run checkbox cell widget for ``row``."""
        cb = QCheckBox()
        # Set the state before connecting so this programmatic init doesn't
        # fire selection_changed (populate is suppressed anyway, belt-and-braces).
        cb.setChecked(checked)
        cb.toggled.connect(self._on_check_toggled)
        self._run_checks[row] = cb
        self.setCellWidget(row, _COL_RUN, _CheckCell(cb))

    def _is_row_checked(self, row: int) -> bool:
        cb = self._run_checks.get(row)
        return cb is not None and cb.isChecked()

    def _set_row_checked(self, row: int, checked: bool) -> None:
        cb = self._run_checks.get(row)
        if cb is not None:
            cb.setChecked(checked)

    def _bulk_set(self, rows: list[int], checked: bool) -> None:
        self._suppress_signal = True
        try:
            for row in rows:
                self._set_row_checked(row, checked)
        finally:
            self._suppress_signal = False
        self.selection_changed.emit(self.selected_count())

    def _on_check_toggled(self, _checked: bool) -> None:
        if self._suppress_signal:
            return
        self.selection_changed.emit(self.selected_count())

    def _set(self, row: int, col: int, text: str) -> None:
        # Only call setItem() when actually inserting a NEW item — calling
        # it on an already-owned QTableWidgetItem produces the Qt warning
        # "cannot insert an item that is already owned by another
        # QTableWidget" and (more importantly) discards the visual update.
        item = self.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            self.setItem(row, col, item)
        item.setText(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _set_status(self, row: int, colour_hex: str, label: str) -> None:
        item = self.item(row, _COL_STATUS)
        if item is None:
            item = QTableWidgetItem()
            self.setItem(row, _COL_STATUS, item)
        item.setText(label)
        item.setForeground(QBrush(QColor("#ffffff")))
        item.setBackground(QBrush(QColor(colour_hex)))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
