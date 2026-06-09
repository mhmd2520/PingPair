"""Modal dialog for the Analysis-tab Export comparison report (#12).

Asks the user for: destination folder, basename, and which formats to
write. Defaults pull from the Save Options tab's Save settings so the two
flows feel coherent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..reporting import ALL_FORMATS, ReportFormat


@dataclass(frozen=True, slots=True)
class ExportComparisonResult:
    """The user's choices, captured when the dialog is accepted."""

    destination_dir: Path
    basename: str
    formats: tuple[ReportFormat, ...]
    notes: str


class ExportComparisonDialog(QDialog):
    """Three-section modal: Destination · Basename + notes · Formats."""

    def __init__(
        self,
        *,
        default_dir: Path,
        default_basename: str,
        default_formats: tuple[ReportFormat, ...] = ALL_FORMATS,
        n_runs: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export comparison report")
        self.setModal(True)
        self.resize(560, 360)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        outer.addWidget(QLabel(
            f"Exporting a comparison report covering <b>{n_runs}</b> "
            "ticked run(s). The active Analysis-tab filter will be "
            "applied to every metric."
        ))

        # ---- Destination ----
        dest_box = QGroupBox("Destination")
        dest_layout = QHBoxLayout(dest_box)
        self._dir_edit = QLineEdit(str(default_dir))
        dest_layout.addWidget(self._dir_edit, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        dest_layout.addWidget(browse_btn)
        outer.addWidget(dest_box)

        # ---- Basename + notes ----
        meta_box = QGroupBox("Filename & notes")
        meta_form = QFormLayout(meta_box)
        self._basename_edit = QLineEdit(default_basename)
        self._basename_edit.setToolTip(
            "Folder + file basename. Each format gets its own file inside "
            "<basename>/<basename>.<ext>."
        )
        meta_form.addRow("Basename:", self._basename_edit)
        self._notes_edit = QLineEdit("")
        self._notes_edit.setPlaceholderText(
            "(optional) Free-text notes embedded in the report header."
        )
        meta_form.addRow("Notes:", self._notes_edit)
        outer.addWidget(meta_box)

        # ---- Formats ----
        fmt_box = QGroupBox("Output formats")
        fmt_layout = QHBoxLayout(fmt_box)
        self._fmt_checks: dict[ReportFormat, QCheckBox] = {}
        for fmt in ALL_FORMATS:
            cb = QCheckBox(fmt)
            cb.setChecked(fmt in default_formats)
            self._fmt_checks[fmt] = cb
            fmt_layout.addWidget(cb)
        fmt_layout.addStretch(1)
        outer.addWidget(fmt_box)

        # ---- Buttons ----
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(
            QDialogButtonBox.StandardButton.Save
        ).setText("Export")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        outer.addWidget(button_box)

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Pick destination folder", self._dir_edit.text() or "",
        )
        if chosen:
            self._dir_edit.setText(chosen)

    def result_value(self) -> ExportComparisonResult | None:
        """Read the form into a result dataclass; ``None`` if user cancelled.

        Call only after ``exec()`` — otherwise widgets may not be in
        their final state. Returns ``None`` when validation fails so
        the caller can re-show the dialog.
        """
        basename = self._basename_edit.text().strip()
        if not basename:
            return None
        formats = tuple(
            fmt for fmt, cb in self._fmt_checks.items() if cb.isChecked()
        )
        if not formats:
            return None
        dest = Path(self._dir_edit.text().strip() or ".")
        return ExportComparisonResult(
            destination_dir=dest,
            basename=basename,
            formats=formats,
            notes=self._notes_edit.text().strip(),
        )
