"""Group C-1 — between-segments dialog for continuous (multi-segment) mode.

Shown by ``_ClientPanel`` after each segment's sweep finishes. The
operator picks one of three actions:

* **Continue with next segment** — store the just-finished segment,
  prompt for a label, kick off a new sweep against the (assumed
  reconnected) Server.
* **Save and finish** — roll up the segments collected so far into a
  multi-segment report and end the run.
* **Retry this segment** — discard the just-finished segment (or
  replace it) and re-run the same plan against the same Server. Only
  enabled when the last segment didn't end OK.

The dialog is Qt-only: pure data in, pure data out. The orchestration
state machine lives on the Client panel — this dialog just gathers
the operator's intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.control.client import SweepSegment
from ..reporting.run_report import fmt_duration


def segment_display_name(idx: int, label: str) -> str:
    """Human-readable name for a sweep segment.

    Collapses to a plain ``Segment N`` when ``label`` is blank or is
    just the auto-default ``Segment N``; otherwise ``Segment N
    (label)``. Avoids the "Segment 1 (Segment 1)" doubling when the
    operator leaves the segment-label field empty.
    """
    label = (label or "").strip()
    base = f"Segment {idx}"
    if not label or label == base:
        return base
    return f"{base} ({label})"


class SegmentDecision(str, Enum):
    """What the operator picked in the between-segments dialog."""

    CONTINUE = "continue"
    SAVE = "save"
    RETRY = "retry"
    # Task U (2026-05-13): record the just-finished segment as-is and
    # move on to the next, WITHOUT re-running. Useful when one car-pair
    # is iffy but the operator wants to keep going through the train.
    SKIP = "skip"


@dataclass(slots=True)
class SegmentDialogResult:
    """Return value from :meth:`BetweenSegmentsDialog.collect_result`.

    ``next_label`` is only meaningful when ``decision == CONTINUE`` —
    for ``RETRY`` the panel reuses the just-finished segment's label,
    and for ``SAVE`` no further label is needed.
    """

    decision: SegmentDecision
    next_label: str = ""


class BetweenSegmentsDialog(QDialog):
    """Modal dialog asking what to do after a segment finishes.

    Construct with the segments collected so far (most recent last)
    and ``last_segment_status`` reflecting that segment's outcome.
    Call :meth:`exec` to show it, then read the operator's choice via
    :meth:`collect_result`.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        completed_segments: list[SweepSegment],
        last_segment_status: str = "ok",
    ) -> None:
        super().__init__(parent)
        self._completed = list(completed_segments)
        self._last_status = last_segment_status
        # Default to CONTINUE — matches the train workflow where the
        # operator usually wants to keep going. If they hit Esc instead
        # of clicking we treat that as SAVE (cleanest "I'm done").
        self._decision: SegmentDecision = SegmentDecision.SAVE
        self._build()

    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.setWindowTitle("Segment complete — next?")
        self.setMinimumWidth(560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        # ----- header -----
        n = len(self._completed)
        last = self._completed[-1] if self._completed else None
        if last is not None:
            ok_word = {
                "ok": "<span style='color:#4caf50;'>OK</span>",
                "partial": "<span style='color:#ffa726;'>finished with errors</span>",
                "failed": "<span style='color:#ef5350;'>failed</span>",
            }.get(self._last_status, self._last_status)
            outer.addWidget(QLabel(
                f"<h3>{segment_display_name(last.segment_idx, last.label)}"
                f" — {ok_word}</h3>"
            ))
            outer.addWidget(QLabel(
                f"<b>{last.cases_ok}/{last.cases_total}</b> cases ok · "
                f"duration <b>{fmt_duration(last.sweep.duration_s)}</b>"
            ))
        else:
            outer.addWidget(QLabel("<h3>Segment complete</h3>"))

        # ----- running tally of segments so far -----
        if self._completed:
            outer.addWidget(self._build_tally_box())

        # ----- next-segment label input -----
        next_idx = n + 1
        form_box = QGroupBox(f"Next segment (Segment {next_idx})")
        form = QFormLayout(form_box)
        self._next_label_edit = QLineEdit()
        self._next_label_edit.setPlaceholderText(f"Segment {next_idx}")
        self._next_label_edit.setToolTip(
            "Free-text identifier for the upcoming segment, e.g. "
            "'Cab M4 ↔ M6'. Press Enter to accept the placeholder."
        )
        form.addRow("Label:", self._next_label_edit)
        outer.addWidget(form_box)

        # ----- buttons -----
        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        self._save_btn = QPushButton("Save and finish")
        self._save_btn.setToolTip(
            "End the multi-segment run and write the consolidated report."
        )
        self._save_btn.clicked.connect(self._on_save)

        self._retry_btn = QPushButton("Retry this segment")
        self._retry_btn.setToolTip(
            "Re-run the previous segment against the Server. The previous "
            "result is dropped from the report."
        )
        self._retry_btn.clicked.connect(self._on_retry)
        # Retry is only useful when the previous segment didn't end OK.
        # Defensive: also disable it if there is no previous segment.
        self._retry_btn.setEnabled(
            last is not None and self._last_status != "ok"
        )

        self._skip_btn = QPushButton("Skip this segment")
        self._skip_btn.setToolTip(
            "Record this segment as-is in the report and advance to the "
            "next one — no retry. Useful when one car-pair is broken but "
            "you want to keep going through the train."
        )
        self._skip_btn.clicked.connect(self._on_skip)

        self._continue_btn = QPushButton("Continue with next segment")
        self._continue_btn.setDefault(True)
        self._continue_btn.setAutoDefault(True)
        self._continue_btn.setToolTip(
            "Save the current segment, plug into the next car-pair, and "
            "the Client will reconnect when you press Continue."
        )
        self._continue_btn.clicked.connect(self._on_continue)

        button_row.addWidget(self._save_btn)
        button_row.addWidget(self._retry_btn)
        button_row.addWidget(self._skip_btn)
        button_row.addStretch(1)
        button_row.addWidget(self._continue_btn)
        outer.addLayout(button_row)

        # Focus the label input so the operator can start typing
        # immediately on most invocations.
        self._next_label_edit.setFocus()

    def _build_tally_box(self) -> QGroupBox:
        box = QGroupBox(f"Completed segments ({len(self._completed)})")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 6)
        tally = QPlainTextEdit()
        tally.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        tally.setFont(mono)
        tally.setMaximumHeight(110)

        lines: list[str] = []
        for seg in self._completed:
            label = seg.label or f"Segment {seg.segment_idx}"
            status_glyph = {
                "ok": "✓", "partial": "!", "failed": "✗",
            }.get(seg.status, "?")
            lines.append(
                f" {status_glyph}  #{seg.segment_idx:>2}  "
                f"{label:<28}  "
                f"{seg.cases_ok}/{seg.cases_total} cases · "
                f"{fmt_duration(seg.sweep.duration_s)}"
            )
        tally.setPlainText("\n".join(lines))
        layout.addWidget(tally)
        return box

    # ------------------------------------------------------------------

    def _on_continue(self) -> None:
        self._decision = SegmentDecision.CONTINUE
        self.accept()

    def _on_save(self) -> None:
        self._decision = SegmentDecision.SAVE
        self.accept()

    def _on_retry(self) -> None:
        self._decision = SegmentDecision.RETRY
        self.accept()

    def _on_skip(self) -> None:
        self._decision = SegmentDecision.SKIP
        self.accept()

    # ------------------------------------------------------------------

    def collect_result(self) -> SegmentDialogResult:
        """Return the operator's choice. Call after :meth:`exec`."""
        # Strip whitespace; if the input is empty the panel falls back
        # to "Segment N" using the placeholder convention.
        next_label = self._next_label_edit.text().strip()
        return SegmentDialogResult(
            decision=self._decision,
            next_label=next_label,
        )
