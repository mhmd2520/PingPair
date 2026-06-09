"""Filter group box used by the Analysis tab.

Extracted from :mod:`analysis_view` to keep that module under the
file-size threshold the cowork session's file-sync layer can write
reliably. Pure Qt — no business logic; emits one ``filters_changed``
signal whenever any of the constituent widgets change.

Round-22 (GGG): the per-value payload / bandwidth tick-boxes and the
Case# spin-box range were dropped. They overlapped on a short left pane
and added clutter; the focus of this tab is the loaded-run list and the
chart. Payload / bandwidth are now plain integer-list input fields
(blank = keep everything), validated by the same ``attach_int_list``
handler the Config tab uses — digits + commas only.
"""

from __future__ import annotations

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..analysis import CasePoint, FilterDescription, LoadedRun
from ._validators import attach_int_list

# Metadata keys exposed in the filter group — compact subset of
# METADATA_LABELS, chosen for highest selectivity in practice.
_FILTER_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("technician", "Technician"),
    ("customer", "Customer"),
    ("record_id", "Record ID"),
)

# Shared fixed label width so every filter row's input field lines up.
_LABEL_W = 124


def _parse_int_set(text: str) -> set[int]:
    """Parse a comma/space-separated integer list, dropping junk tokens.

    Blank or all-junk input yields the empty set, which the predicates
    treat as 'match everything' (no filter). The field's keypress filter
    already restricts input to digits / commas / spaces, so this is just
    a defensive final parse.
    """
    out: set[int] = set()
    for tok in text.replace(",", " ").split():
        try:
            out.add(int(tok))
        except ValueError:
            continue
    return out


class AnalysisFilters(QGroupBox):
    """Compound widget — emits ``filters_changed`` when any input changes."""

    filters_changed = Signal()

    def __init__(self) -> None:
        super().__init__("Filters (refine the plot)")
        self.setToolTip(
            "All filters AND together. Empty filters are pass-throughs. "
            "Cases excluded by the payload / bandwidth filter are skipped "
            "in the plot; runs excluded by the metadata filter are greyed "
            "out in the list above."
        )
        outer = QVBoxLayout(self)
        # Round-25 (point 11): tighter spacing so Filters takes a little less
        # vertical room, leaving more for the taller Loaded-runs list above.
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        # ---- Payload filter (integer list; blank = all) ----
        self._payload_edit = self._build_axis_row(
            outer,
            "Payloads (B):",
            "e.g. 200, 600 — blank = all",
            "Payload sizes to keep, e.g. 200, 600, 1000. Comma-separated "
            "integers; blank = every payload. Digits only.",
        )
        # ---- Bandwidth filter (integer list; blank = all) ----
        self._bandwidth_edit = self._build_axis_row(
            outer,
            "Bandwidths (Mbps):",
            "e.g. 10, 30 — blank = all",
            "Bandwidths to keep, e.g. 10, 30, 90. Comma-separated "
            "integers; blank = every bandwidth. Digits only.",
        )

        # ---- Metadata text filters ----
        # Explicit fixed-width-label + field rows, NOT a QFormLayout — a
        # QFormLayout starves vertically when the left pane is short until
        # the labels overlap their inputs (Round-21 ZZ). A QHBoxLayout per
        # row never collapses, at any height.
        self._metadata_edits: dict[str, QLineEdit] = {}
        for key, label in _FILTER_METADATA_FIELDS:
            meta_row = QHBoxLayout()
            cap = QLabel(f"{label}:")
            cap.setFixedWidth(_LABEL_W)
            meta_row.addWidget(cap)
            edit = QLineEdit()
            edit.setPlaceholderText("(any)")
            edit.setToolTip(
                f"Substring match (case-insensitive) on the {label!r} "
                "metadata field. Empty = match any value."
            )
            edit.textChanged.connect(self._emit_changed)
            self._metadata_edits[key] = edit
            meta_row.addWidget(edit, stretch=1)
            outer.addLayout(meta_row)

        # Round-24 (MMM): the "Reset filters" button was removed — every field
        # is blank-by-default and trivially cleared by hand, so the button was
        # clutter on a busy left pane. The ``reset_all`` slot is kept as public
        # API (callers can still wipe filters programmatically).

    def _build_axis_row(
        self,
        outer: QVBoxLayout,
        label: str,
        placeholder: str,
        tooltip: str,
    ) -> QLineEdit:
        """Build one 'label: <integer-list field>' row and return the field."""
        row = QHBoxLayout()
        cap = QLabel(label)
        cap.setFixedWidth(_LABEL_W)
        row.addWidget(cap)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        # Same validator/handler the Config tab uses — digits + commas only,
        # blank allowed (= keep everything).
        attach_int_list(edit, tooltip, allow_blank=True)
        edit.textChanged.connect(self._emit_changed)
        row.addWidget(edit, stretch=1)
        outer.addLayout(row)
        return edit

    def _emit_changed(self, *_args) -> None:
        self.filters_changed.emit()

    @Slot()
    def reset_all(self) -> None:
        """Wipe every widget back to 'match everything' default, emit once."""
        widgets = [
            self._payload_edit,
            self._bandwidth_edit,
            *self._metadata_edits.values(),
        ]
        for w in widgets:
            w.blockSignals(True)
        try:
            for edit in widgets:
                edit.setText("")
        finally:
            for w in widgets:
                w.blockSignals(False)
        self.filters_changed.emit()

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

    def case_passes(self, case: CasePoint) -> bool:
        """True iff ``case`` satisfies the payload / bandwidth filter.

        Each field is an integer allow-list; blank = keep every value.
        """
        payloads = _parse_int_set(self._payload_edit.text())
        bandwidths = _parse_int_set(self._bandwidth_edit.text())
        ok_payload = not payloads or case.payload_bytes in payloads
        ok_bandwidth = not bandwidths or case.bandwidth_mbps_pushed in bandwidths
        return ok_payload and ok_bandwidth

    def run_passes_metadata(self, run: LoadedRun) -> bool:
        """True iff every non-empty metadata filter substring-matches ``run``."""
        for key, edit in self._metadata_edits.items():
            needle = edit.text().strip().lower()
            if not needle:
                continue
            haystack = (run.metadata.get(key) or "").lower()
            if needle not in haystack:
                return False
        return True

    def describe_active(self) -> str:
        """One-line, human-readable summary of the active filters.

        Used in the exported comparison report so the reader knows which
        cases / runs the charts reflect. Returns a friendly 'no filters'
        string when nothing is constraining the plot.
        """
        parts: list[str] = []
        payloads = _parse_int_set(self._payload_edit.text())
        if payloads:
            parts.append(
                "payloads " + ", ".join(str(p) for p in sorted(payloads)) + " B"
            )
        bandwidths = _parse_int_set(self._bandwidth_edit.text())
        if bandwidths:
            parts.append(
                "bandwidths "
                + ", ".join(str(b) for b in sorted(bandwidths))
                + " Mbps"
            )
        for key, label in _FILTER_METADATA_FIELDS:
            needle = self._metadata_edits[key].text().strip()
            if needle:
                parts.append(f"{label} contains {needle!r}")
        return "; ".join(parts) if parts else "no filters (all cases)"

    def filter_description(self) -> FilterDescription:
        """Structured snapshot of the active filters for the comparison report.

        The comparison-report writers dereference
        ``report.filter_description`` as a :class:`FilterDescription`
        object (``.is_default`` / ``.lines()``) — passing the *string*
        from :meth:`describe_active` here raised ``AttributeError`` and
        broke the Export-comparison-report feature in every format. This
        builds the object the writers expect from the live widgets.

        There is no case-range widget on this tab (it was dropped in
        Round-22), so the case range is always the full sweep — we leave
        ``case_lo=1`` / ``case_hi=20`` so the writers suppress the "Cases:"
        line. ``is_default`` is True only when no payload / bandwidth /
        metadata filter is constraining the plot.
        """
        payloads = tuple(sorted(_parse_int_set(self._payload_edit.text())))
        bandwidths = tuple(sorted(_parse_int_set(self._bandwidth_edit.text())))
        metadata: list[tuple[str, str]] = []
        for key, label in _FILTER_METADATA_FIELDS:
            needle = self._metadata_edits[key].text().strip()
            if needle:
                metadata.append((label, needle))
        is_default = not (payloads or bandwidths or metadata)
        return FilterDescription(
            case_lo=1,
            case_hi=20,
            payloads=payloads,
            bandwidths=bandwidths,
            metadata=tuple(metadata),
            is_default=is_default,
        )
