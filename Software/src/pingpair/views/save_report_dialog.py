"""Group C-1 follow-up — post-test "Save report?" dialog.

Shown by ``_ClientPanel`` after every test (single-sweep or multi-
segment) **when Auto-save is OFF**. Combines the result summary with
an interactive save form so the operator can:

* Confirm where to save (defaults to the Save Options tab's Destination
  Folder, can be overridden via Browse).
* Confirm the filename pattern (defaults to the Save Options tab's
  pattern, can be tweaked per run).
* Tick "Don't ask me in the future" to flip Auto-save ON permanently
  — the dialog's Destination + Pattern then become the saved defaults.
* Click **Skip** to discard the run (no files written; the status
  line on the Client panel reminds them they can manually save
  later from the Save Options tab's "Save report now" button).

Qt-only. The orchestration (which save helper to call, where to
update RunState) lives on the Client panel — this dialog just
gathers the operator's intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SaveDialogDecision(str, Enum):
    """What the operator picked in the post-test save dialog."""

    SAVE = "save"
    SKIP = "skip"


@dataclass(slots=True)
class SaveDialogResult:
    """Return value from :meth:`SaveReportDialog.collect_result`."""

    decision: SaveDialogDecision
    destination_dir: Path
    filename_pattern: str
    remember: bool   # True when "Don't ask me in the future" was ticked


class SaveReportDialog(QDialog):
    """Post-test save prompt for the prompt-first save flow.

    Construct with the result summary already formatted as a one-line
    string (e.g. "1/1 cases ok · 51s" for single sweeps,
    "2/2 segments ok · 4/4 cases ok · 5m 31s" for multi-segment).
    The dialog pre-fills the Destination + Pattern from the values
    passed in; call :meth:`exec` to show it, then read
    :meth:`collect_result` for the operator's choice.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        result_summary: str,
        default_destination: Path,
        default_pattern: str,
        is_multi_segment: bool = False,
    ) -> None:
        super().__init__(parent)
        self._is_multi_segment = is_multi_segment
        self._result_summary = result_summary
        self._default_destination = Path(default_destination)
        self._default_pattern = default_pattern
        # Default decision is SKIP — if the operator dismisses via Esc
        # or the window close button, we treat that as "discard".
        self._decision: SaveDialogDecision = SaveDialogDecision.SKIP
        self._build()

    # ------------------------------------------------------------------

    def _build(self) -> None:
        title = (
            "Multi-segment run complete — save report?"
            if self._is_multi_segment
            else "Sweep complete — save report?"
        )
        self.setWindowTitle(title)
        self.setMinimumWidth(620)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        # ----- result summary banner -----
        kind_word = "Multi-segment run" if self._is_multi_segment else "Sweep"
        outer.addWidget(QLabel(f"<h3>{kind_word} complete</h3>"))
        result_lbl = QLabel(f"<b>{self._result_summary}</b>")
        result_lbl.setStyleSheet("color:#4caf50;")  # green
        outer.addWidget(result_lbl)
        outer.addWidget(QLabel(
            "Pick a destination and filename pattern below, then click "
            "<b>Save</b>. Click <b>Skip</b> if you don't want to save "
            "this run."
        ))

        # ----- save form -----
        form_box = QGroupBox("Save settings")
        form = QFormLayout(form_box)

        dest_row = QHBoxLayout()
        dest_row.setContentsMargins(0, 0, 0, 0)
        dest_row.setSpacing(6)
        self._dest_edit = QLineEdit(str(self._default_destination))
        self._dest_edit.setToolTip(
            "Folder where the per-run subfolder will be created. The "
            "subfolder name is derived from the Filename pattern below."
        )
        dest_row.addWidget(self._dest_edit, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        dest_row.addWidget(browse_btn)
        dest_wrap = QWidget()
        dest_wrap.setLayout(dest_row)
        form.addRow("Destination folder:", dest_wrap)

        self._pattern_edit = QLineEdit(self._default_pattern)
        self._pattern_edit.setToolTip(
            "Filename stem before the extension. Use {date} and {time} "
            "as tokens. Example: PingPair_{date}_{time} → "
            "PingPair_2026-05-11_142359"
        )
        form.addRow("Filename pattern:", self._pattern_edit)

        token_hint = QLabel(
            "Tokens: <code>{date}</code> → today's date "
            "(YYYY-MM-DD), <code>{time}</code> → current time (HHMMSS). "
            "Multi-segment runs append '_multisegment' automatically."
        )
        token_hint.setStyleSheet("color:#888;")
        token_hint.setWordWrap(True)
        form.addRow("", token_hint)

        outer.addWidget(form_box)

        # ----- remember preference -----
        self._remember_check = QCheckBox(
            "Don't ask me in the future "
            "— use Auto-save with the settings above for every future test"
        )
        self._remember_check.setToolTip(
            "Ticking this and clicking Save turns on Auto-save in the "
            "Save Options tab, with the Destination + Pattern above as the "
            "saved defaults. You can flip it back off any time from the "
            "Save Options tab."
        )
        outer.addWidget(self._remember_check)

        # ----- buttons -----
        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        self._skip_btn = QPushButton("Skip")
        self._skip_btn.setToolTip(
            "Discard this run — don't write any report files. You can "
            "still save it later from the Save Options tab's 'Save report now' "
            "button (while the run is in memory)."
        )
        self._skip_btn.clicked.connect(self._on_skip)

        self._save_btn = QPushButton("Save")
        self._save_btn.setDefault(True)
        self._save_btn.setAutoDefault(True)
        self._save_btn.setToolTip(
            "Write the report files to the destination above and add "
            "the run to the Recent reports list."
        )
        self._save_btn.clicked.connect(self._on_save)

        button_row.addWidget(self._skip_btn)
        button_row.addStretch(1)
        button_row.addWidget(self._save_btn)
        outer.addLayout(button_row)

        # Focus the Save button so Enter saves and Esc skips —
        # the most common path on a successful run.
        self._save_btn.setFocus()

    # ------------------------------------------------------------------

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Choose destination folder",
            self._dest_edit.text() or str(self._default_destination),
        )
        if chosen:
            self._dest_edit.setText(chosen)

    def _on_save(self) -> None:
        # Defensive: an empty destination doesn't make sense — fall
        # back to the default rather than refusing to save.
        if not self._dest_edit.text().strip():
            self._dest_edit.setText(str(self._default_destination))
        # Same for pattern.
        if not self._pattern_edit.text().strip():
            self._pattern_edit.setText(self._default_pattern)
        self._decision = SaveDialogDecision.SAVE
        self.accept()

    def _on_skip(self) -> None:
        self._decision = SaveDialogDecision.SKIP
        self.accept()

    # ------------------------------------------------------------------

    def collect_result(self) -> SaveDialogResult:
        """Return the operator's choice. Call after :meth:`exec`."""
        return SaveDialogResult(
            decision=self._decision,
            destination_dir=Path(
                self._dest_edit.text().strip() or str(self._default_destination)
            ),
            filename_pattern=(
                self._pattern_edit.text().strip() or self._default_pattern
            ),
            remember=self._remember_check.isChecked(),
        )
