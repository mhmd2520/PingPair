"""Save Options tab — output destination, filename pattern, format selection.

Drives the Phase 4 report writer.  Reads the most recent SweepResult
from ``ctx.run_state.last_sweep_result`` (set by the Run tab when a
sweep completes) and either auto-saves it after the sweep or saves on
demand via the "Save report now" button.

Also scans the configured destination folder on tab activation so the
"Last sweep" + "Recent reports" sections survive an app restart — they
populate from any ``.json`` sidecars already on disk.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtWidgets import QPlainTextEdit

from ..context import AppContext
from ..reporting import (
    ALL_FORMATS,
    ReportFormat,
    build_multi_run_report,
    build_run_report,
    render_filename,
    save_report,
    unique_basename,
)
from ..reporting.run_report import METADATA_LABELS
from ._base import BaseView, _shape_input
from ._validators import attach_filename_safe, attach_path_safe
from .about_view import _open_in_file_browser


_FORMAT_LABELS: dict[str, str] = {
    "docx": "Word (.docx)",
    "xlsx": "Excel (.xlsx)",
    "pdf":  "PDF (.pdf)",
    "txt":  "Text (.txt)",
}


class ReportView(BaseView):
    title = "Save Options — output settings"

    def _build_placeholder(self) -> None:
        # The Save Options tab stacks four group boxes (Save settings, metadata,
        # last sweep, recent reports) — taller than a short window. Wrap
        # the content in a scroll area so it scrolls instead of the forms
        # compressing into each other (the overlap bug seen on the VM).
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)

        outer = QVBoxLayout(content)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        outer.addWidget(QLabel(f"<h2>{self.title}</h2>"))
        intro = QLabel(
            "Choose how reports are saved: <b>Auto save</b> off prompts you each "
            "run; on, it saves automatically. A <code>.json</code> sidecar "
            "always accompanies it."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # ---- Save settings -------------------------------------------------
        settings_box = QGroupBox("Save settings")
        form = QFormLayout(settings_box)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        # Auto-save master toggle — first so its dependents read clearly.
        self._auto_check = QCheckBox(
            "Auto save  —  write reports automatically without asking"
        )
        self._auto_check.setToolTip(
            "When ticked, every test auto-saves to the Destination + "
            "Filename pattern below. When unticked, PingPair prompts "
            "you after each test (with a 'Don't ask me in the future' "
            "option to switch back to auto-save)."
        )
        self._auto_check.setChecked(self.ctx.run_state.report_auto_save)
        self._auto_check.toggled.connect(self._on_auto_toggled)
        form.addRow("", self._auto_check)

        # Destination folder — only editable when Auto save is on; otherwise
        # it's the default that pre-fills the post-test save dialog.
        dir_row = QHBoxLayout()
        self._dir_edit = QLineEdit(str(self.ctx.run_state.report_dir))
        _shape_input(self._dir_edit)
        attach_path_safe(
            self._dir_edit,
            "Destination folder for saved reports. Forbidden: < > | \" * ?",
        )
        self._dir_edit.editingFinished.connect(self._on_dir_changed)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.clicked.connect(self._on_browse)
        dir_row.addWidget(self._dir_edit, stretch=1)
        dir_row.addWidget(self._browse_btn)
        form.addRow("Destination folder:", dir_row)

        # Filename pattern — same disabled-when-auto-off treatment.
        self._name_edit = QLineEdit(self.ctx.run_state.report_filename_pattern)
        _shape_input(self._name_edit)
        attach_filename_safe(
            self._name_edit,
            "Filename pattern. Tokens {date} / {time} expanded at save time.\n"
            "Forbidden: < > | \" * ? : / \\",
        )
        self._name_edit.editingFinished.connect(self._on_name_changed)
        form.addRow("Filename pattern:", self._name_edit)

        self._tokens_hint = QLabel(
            "Tokens: <code>{date}</code> → 2026-05-09 · "
            "<code>{time}</code> → 0947"
        )
        self._tokens_hint.setStyleSheet("color:#888;")
        form.addRow("", self._tokens_hint)

        # Format checkboxes apply to BOTH auto-save and the post-test
        # save dialog (the dialog uses whichever formats are ticked here)
        # — so they stay editable regardless of Auto save state. All five
        # export options share ONE row with a wide inter-item gap so each
        # checkbox reads as paired with its OWN label, not the previous one.
        formats_row = QHBoxLayout()
        # Wide-ish gap so each box pairs with its OWN label, but compact enough
        # that all five toggles fit on one line at the minimum window width
        # (28 px overflowed and clipped the last toggle).
        formats_row.setSpacing(16)
        self._format_boxes: dict[str, QCheckBox] = {}
        selected = set(self.ctx.run_state.report_formats)
        for fmt in ALL_FORMATS:
            cb = QCheckBox(_FORMAT_LABELS[fmt])
            cb.setChecked(fmt in selected)
            cb.toggled.connect(self._on_format_toggled)
            self._format_boxes[fmt] = cb
            formats_row.addWidget(cb)

        # Charts (PNG) toggle — a SEPARATE setting (report_include_chart_pngs,
        # not report_formats) but shown on the same export-options row so all
        # the toggles live on one line. When ticked, every saved report also
        # writes an Analysis_Images/ subfolder of matplotlib metric +
        # breakdown PNGs.
        self._charts_check = QCheckBox("Charts (.png)")
        self._charts_check.setToolTip(
            "Saves the matplotlib-rendered metric line + breakdown bar "
            "charts as PNGs into an Analysis_Images/ subfolder next to "
            "the docx/xlsx/pdf/txt files. Disable if you don't need "
            "standalone images."
        )
        self._charts_check.setChecked(
            getattr(self.ctx.run_state, "report_include_chart_pngs", True)
        )
        self._charts_check.toggled.connect(self._on_charts_toggled)
        formats_row.addWidget(self._charts_check)

        formats_row.addStretch(1)
        form.addRow("Formats:", formats_row)

        # Right-aligned Reset row — wipes the Save-settings group back
        # to the factory defaults (Auto save off, default Destination,
        # default Pattern, default Formats). Metadata + Recent reports
        # are deliberately left alone — only what's in the Save
        # settings box is reset.
        reset_row = QHBoxLayout()
        reset_row.addStretch(1)
        self._reset_btn = QPushButton("Reset to defaults")
        self._reset_btn.setToolTip(
            "Reset Auto save, Destination folder, Filename pattern, and "
            "Formats back to their out-of-the-box defaults. Doesn't touch "
            "Test-record metadata or the Recent reports list."
        )
        self._reset_btn.clicked.connect(self._on_reset_save_settings)
        reset_row.addWidget(self._reset_btn)
        form.addRow("", reset_row)

        outer.addWidget(settings_box)

        # Apply the initial enabled/disabled state of the dependent
        # fields. Called explicitly so the toggled signal doesn't have
        # to fire just to set things up.
        self._apply_auto_state(self._auto_check.isChecked())

        # Register for cross-view save-settings notifications so the
        # post-test SaveReportDialog's changes (Destination / Pattern /
        # Auto save flip from "Don't ask me in the future") flow into
        # our widgets immediately — no tab switch required.
        self.ctx.save_settings_listeners.append(
            self._refresh_save_settings_from_state
        )

        # ---- Test-record metadata ----------------------------------------
        meta_box = QGroupBox("Test-record metadata (optional)")
        meta_form = QFormLayout(meta_box)
        meta_form.setVerticalSpacing(8)
        meta_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        meta_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        _meta_intro = QLabel(
            "Populated fields appear on every report's title page / Run-info "
            "sheet. Leave blank to omit. Saved automatically across launches."
        )
        _meta_intro.setWordWrap(True)
        meta_form.addRow(_meta_intro)
        self._metadata_edits: dict[str, QLineEdit | QPlainTextEdit] = {}
        existing = getattr(self.ctx.run_state, "report_metadata", None) or {}
        for key, label in METADATA_LABELS:
            if key == "environment":
                # Multi-line for the environment notes.
                widget = QPlainTextEdit()
                widget.setPlainText(existing.get(key, ""))
                widget.setMinimumHeight(48)
                widget.setMaximumHeight(60)
                widget.textChanged.connect(self._on_metadata_changed)
            else:
                widget = QLineEdit(existing.get(key, ""))
                _shape_input(widget)
                widget.editingFinished.connect(self._on_metadata_changed)
            self._metadata_edits[key] = widget
            meta_form.addRow(f"{label}:", widget)
        outer.addWidget(meta_box)

        # ---- Last sweep ---------------------------------------------------
        last_box = QGroupBox("Last sweep")
        last_layout = QVBoxLayout(last_box)
        self._last_label = QLabel("(no sweep finished in this session yet)")
        self._last_label.setStyleSheet("color:#aaa;")
        last_layout.addWidget(self._last_label)

        last_buttons = QHBoxLayout()
        self._save_now_btn = QPushButton("Save report now")
        self._save_now_btn.setEnabled(False)
        self._save_now_btn.clicked.connect(self._on_save_now)
        last_buttons.addWidget(self._save_now_btn)
        last_buttons.addStretch(1)
        last_layout.addLayout(last_buttons)

        outer.addWidget(last_box)

        # ---- Recent reports -----------------------------------------------
        recent_box = QGroupBox("Recent reports (from destination folder)")
        recent_layout = QVBoxLayout(recent_box)
        self._recent_list = QListWidget()
        self._recent_list.setMinimumHeight(120)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._recent_list.setFont(mono)
        recent_layout.addWidget(self._recent_list)

        recent_buttons = QHBoxLayout()
        open_folder_btn = QPushButton("Open destination folder")
        open_folder_btn.clicked.connect(self._on_open_folder)
        recent_buttons.addWidget(open_folder_btn)
        clear_btn = QPushButton("Clear list")
        clear_btn.clicked.connect(self._on_clear_recent)
        recent_buttons.addWidget(clear_btn)
        recent_buttons.addStretch(1)
        recent_layout.addLayout(recent_buttons)

        outer.addWidget(recent_box, stretch=1)

        # Render any state already on the context (e.g. user switches to
        # this tab AFTER a sweep finished while they were on the Run tab).
        self.refresh()

    # ------------------------------------------------------------------
    # Public refresh — called by main window when the tab activates
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        # Save settings might have been changed by the post-test
        # SaveReportDialog while another tab was active — pull them
        # before re-rendering anything else.
        self._refresh_save_settings_from_state()
        # Pull whatever's on disk into recent_reports first so the
        # last-sweep label has data to fall back on.
        self._scan_destination_for_recent()
        self._refresh_last_sweep_label()
        self._refresh_recent_list()

    def _refresh_last_sweep_label(self) -> None:
        sweep = self.ctx.run_state.last_sweep_result
        if sweep is not None:
            # In-session sweep — most authoritative source.
            ok = sum(1 for c in sweep.cases if c.ok)
            total = len(sweep.cases)
            ts = datetime.fromtimestamp(sweep.started_at).strftime("%Y-%m-%d %H:%M:%S")
            self._last_label.setText(
                f"<b>{ts}</b> · {total} cases · {ok}/{total} ok · "
                f"duration {sweep.duration_s:.1f} s · this session"
            )
            self._last_label.setStyleSheet("")
            self._save_now_btn.setEnabled(True)
            self._save_now_btn.setToolTip("")
            return

        # No in-session sweep — fall back to the most recent .json
        # sidecar on disk so the user still sees something meaningful
        # after a restart.  Parsing it gives us run_id / cases /
        # duration.
        latest = self._latest_sidecar_on_disk()
        if latest is not None:
            try:
                data = json.loads(latest.read_text(encoding="utf-8"))
                self._last_label.setText(
                    f"<b>{data.get('run_id', latest.stem)}</b> · "
                    f"{data.get('cases_total', '?')} cases · "
                    f"{data.get('cases_ok', '?')}/"
                    f"{data.get('cases_total', '?')} ok · "
                    f"duration {float(data.get('duration_s', 0)):.1f} s · "
                    "previous session (read from disk)"
                )
                self._last_label.setStyleSheet("color:#aaa;")
                self._save_now_btn.setEnabled(False)
                self._save_now_btn.setToolTip(
                    "Re-saving requires the in-memory SweepResult from a "
                    "completed sweep. Run a new sweep to enable this."
                )
                return
            except (OSError, ValueError, KeyError):
                pass

        self._last_label.setText("(no sweep on disk and none yet this session)")
        self._save_now_btn.setEnabled(False)

    def _refresh_recent_list(self) -> None:
        self._recent_list.clear()
        for path in self.ctx.run_state.recent_reports:
            item = QListWidgetItem(str(path))
            item.setToolTip(str(path))
            self._recent_list.addItem(item)

    # ------------------------------------------------------------------
    # Disk scan: surface previous-session reports
    # ------------------------------------------------------------------

    def _enumerate_sidecars(self) -> list[Path]:
        """Return all sweep sidecars in report_dir.

        Each sweep lives in its own subfolder named after the run; the
        sidecar inside is ``Reports/Ping_xxx/Ping_xxx.json``. Restricted
        to files whose basename matches the parent subfolder so unrelated
        ``.json`` files (notes, profile copies, etc.) are ignored.
        """
        rs = self.ctx.run_state
        if not rs.report_dir.is_dir():
            return []
        sidecars: list[Path] = []
        try:
            for sub in rs.report_dir.iterdir():
                if sub.is_dir():
                    candidate = sub / f"{sub.name}.json"
                    if candidate.is_file():
                        sidecars.append(candidate)
        except OSError:
            return []
        try:
            sidecars.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
        return sidecars

    def _latest_sidecar_on_disk(self) -> Path | None:
        """Most recently-modified .json sidecar under report_dir, or None."""
        sidecars = self._enumerate_sidecars()
        return sidecars[0] if sidecars else None

    def _scan_destination_for_recent(self) -> None:
        """Populate recent_reports from disk so the list survives restarts.

        Walks the per-sweep subfolders.  For each sidecar we add any
        sibling report files that share the basename. In-session
        entries are preserved at the top.
        """
        rs = self.ctx.run_state
        sidecars = self._enumerate_sidecars()[:20]  # last 20 sweeps is plenty

        seen: set[str] = {str(p) for p in rs.recent_reports}
        fresh: list[Path] = list(rs.recent_reports)

        for sidecar in sidecars:
            basename = sidecar.stem
            sweep_dir = sidecar.parent
            siblings: list[Path] = []
            for ext in ("docx", "xlsx", "pdf", "txt"):
                candidate = sweep_dir / f"{basename}.{ext}"
                if candidate.exists():
                    siblings.append(candidate)
            siblings.append(sidecar)

            for path in siblings:
                key = str(path)
                if key not in seen:
                    seen.add(key)
                    fresh.append(path)

        rs.recent_reports = fresh[:50]

    # ------------------------------------------------------------------
    # Settings handlers
    # ------------------------------------------------------------------

    def _on_dir_changed(self) -> None:
        text = self._dir_edit.text().strip()
        if text:
            self.ctx.run_state.report_dir = Path(text)

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Pick report destination folder",
            str(self.ctx.run_state.report_dir),
        )
        if chosen:
            self.ctx.run_state.report_dir = Path(chosen)
            self._dir_edit.setText(chosen)

    def _on_name_changed(self) -> None:
        text = self._name_edit.text().strip()
        if text:
            self.ctx.run_state.report_filename_pattern = text

    def _on_format_toggled(self, _checked: bool) -> None:
        formats: list[str] = [
            fmt for fmt, cb in self._format_boxes.items() if cb.isChecked()
        ]
        self.ctx.run_state.report_formats = formats

    def _on_auto_toggled(self, checked: bool) -> None:
        """Apply the Auto-save toggle + clear any stale invalid state.

        Mirrors the Setup tab override-checkbox behaviour (Group F
        follow-up, Round-17, 2026-05-18): when the user UNticks the
        master toggle we reset Destination + Filename pattern back to
        factory defaults so a stale typo (e.g. ``what?`` in the
        pattern field) doesn't keep flagging red while greyed-out.
        Re-ticking the box then re-enables editable fields in a
        clean state. Tick-state transitions don't reset because the
        user just turned the feature on — keep whatever they typed.
        """
        self.ctx.run_state.report_auto_save = checked
        if not checked:
            # Reset to factory defaults. The setText() calls trigger
            # textChanged so the validators re-evaluate and clear
            # any red invalid state. RunState is updated explicitly
            # so the next save-dialog reads the clean defaults too.
            from ..paths import REPORTS_DIR
            default_dir = self.ctx.config.report.default_dir or REPORTS_DIR
            default_pattern = "PingPair_{date}_{time}"
            self._dir_edit.setText(str(default_dir))
            self._name_edit.setText(default_pattern)
            self.ctx.run_state.report_dir = default_dir
            self.ctx.run_state.report_filename_pattern = default_pattern
        self._apply_auto_state(checked)

    def _on_charts_toggled(self, checked: bool) -> None:
        self.ctx.run_state.report_include_chart_pngs = checked

    @Slot()
    def _on_reset_save_settings(self) -> None:
        """Wipe the Save settings group back to factory defaults.

        Auto save → off, Destination folder → ``REPORTS_DIR`` (the
        bundled default), Filename pattern → ``PingPair_{date}_{time}``,
        Formats → ``["docx", "xlsx"]``. Metadata and Recent reports
        are not touched.

        Confirm dialog first so an accidental click doesn't lose the
        operator's custom Destination.
        """
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle("Reset save settings?")
        confirm.setText(
            "Reset Auto save, Destination folder, Filename pattern, and "
            "Formats to defaults?"
        )
        confirm.setInformativeText(
            "Test-record metadata and the Recent reports list are NOT "
            "affected. You'll need to re-tick 'Don't ask me in the "
            "future' on the next save prompt if you want Auto save back."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        # Pull factory defaults from the same source the AppConfig
        # loader uses, so any future change to the bundled defaults
        # automatically flows here too.
        from ..paths import REPORTS_DIR

        rs = self.ctx.run_state
        rs.report_auto_save = False
        rs.report_dir = (
            self.ctx.config.report.default_dir or REPORTS_DIR
        )
        rs.report_filename_pattern = "PingPair_{date}_{time}"
        # docx + xlsx is the documented factory default (CLAUDE.md §2 Reporting:
        # "docx + xlsx default, PDF / TXT optional"). Was resetting to all four,
        # silently turning PDF + TXT on — contradicting the docstring and the
        # ExportComparisonDialog default.
        rs.report_formats = ["docx", "xlsx"]

        # Push the new values through the cross-view notifier so this
        # view's widgets (and anyone else that registered) all refresh
        # off the fresh RunState in one go.
        self.ctx.notify_save_settings_changed()

    def _refresh_save_settings_from_state(self) -> None:
        """Pull Auto save + Destination + Pattern + Formats from RunState.

        Called whenever something outside the Save Options tab mutates the
        save settings (most commonly: the post-test SaveReportDialog
        when the operator typed a custom Destination + ticked
        "Don't ask me in the future"). Signals on every widget are
        blocked during the update so we don't recursively trigger
        ``_on_dir_changed`` / ``_on_name_changed`` / ``_on_auto_toggled``
        and end up re-saving back into RunState the value we just read.
        """
        rs = self.ctx.run_state

        self._auto_check.blockSignals(True)
        self._dir_edit.blockSignals(True)
        self._name_edit.blockSignals(True)
        for cb in self._format_boxes.values():
            cb.blockSignals(True)
        try:
            self._auto_check.setChecked(rs.report_auto_save)
            self._dir_edit.setText(str(rs.report_dir))
            self._name_edit.setText(rs.report_filename_pattern)
            selected = set(rs.report_formats)
            for fmt, cb in self._format_boxes.items():
                cb.setChecked(fmt in selected)
        finally:
            self._auto_check.blockSignals(False)
            self._dir_edit.blockSignals(False)
            self._name_edit.blockSignals(False)
            for cb in self._format_boxes.values():
                cb.blockSignals(False)
        # Re-apply the enabled/disabled state for the dependent fields
        # since blockSignals stopped the toggled callback from doing it.
        self._apply_auto_state(rs.report_auto_save)

    def _apply_auto_state(self, auto_save_on: bool) -> None:
        """Grey out / enable the Destination + Pattern fields.

        When Auto save is OFF those fields are read-only defaults that
        pre-fill the post-test save dialog — editable per run via the
        dialog itself, not directly from the Save Options tab. Tooltip
        explains this so the user isn't confused why the fields look
        locked.
        """
        self._dir_edit.setEnabled(auto_save_on)
        self._browse_btn.setEnabled(auto_save_on)
        self._name_edit.setEnabled(auto_save_on)
        self._tokens_hint.setEnabled(auto_save_on)

        if auto_save_on:
            tip = ""
        else:
            tip = (
                "Auto save is off — PingPair will prompt for the "
                "destination and filename at the end of every test, "
                "with these values pre-filled. Tick Auto save above "
                "to edit them directly."
            )
        self._dir_edit.setToolTip(tip)
        self._name_edit.setToolTip(tip)
        self._browse_btn.setToolTip(tip)

    def _on_metadata_changed(self, *_args) -> None:
        """Mirror every metadata edit straight into RunState.

        Called from both QLineEdit.editingFinished and QPlainTextEdit
        textChanged, so we don't care which widget triggered us — just
        re-snapshot all of them.
        """
        rs = self.ctx.run_state
        rs.report_metadata = {}
        for key, widget in self._metadata_edits.items():
            if isinstance(widget, QPlainTextEdit):
                rs.report_metadata[key] = widget.toPlainText().strip()
            else:
                rs.report_metadata[key] = widget.text().strip()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @Slot()
    def _on_save_now(self) -> None:
        sweep = self.ctx.run_state.last_sweep_result
        if sweep is None:
            return

        rs = self.ctx.run_state
        # Prompt-first flow when Auto save is off — same dialog the
        # post-test path uses, so the experience is consistent.
        if not rs.report_auto_save:
            from .save_report_dialog import (
                SaveDialogDecision,
                SaveReportDialog,
            )
            ok = sum(1 for c in sweep.cases if c.ok)
            total = len(sweep.cases)
            from ..reporting.run_report import fmt_duration
            summary = (
                f"{ok}/{total} cases ok · {fmt_duration(sweep.duration_s)}"
            )
            dlg = SaveReportDialog(
                self,
                result_summary=summary,
                default_destination=rs.report_dir,
                default_pattern=rs.report_filename_pattern,
                is_multi_segment=False,
            )
            dlg.exec()
            result = dlg.collect_result()
            if result.decision is SaveDialogDecision.SKIP:
                return
            rs.report_dir = result.destination_dir
            rs.report_filename_pattern = result.filename_pattern
            if result.remember:
                rs.report_auto_save = True
            # Refresh the tab's widgets + any other registered
            # listeners so the new values flow everywhere.
            self.ctx.notify_save_settings_changed()

        # Off the GUI thread (shared helper) so the multi-format write +
        # chart renders don't freeze the window — same pattern the post-sweep
        # auto-save uses. (H4, 2026-06-04.)
        from ._qt_runner import run_save_in_background

        written, err = run_save_in_background(
            self, lambda: _save_sweep(self.ctx, sweep), logger=self.ctx.logger
        )
        if err:
            QMessageBox.critical(
                self, "Save failed",
                f"Could not write the report:\n\n{err}",
            )
            return

        QMessageBox.information(
            self, "Report saved",
            "Wrote:\n\n" + "\n".join(str(p) for p in written),
        )
        self._refresh_recent_list()

    @Slot()
    def _on_open_folder(self) -> None:
        path = self.ctx.run_state.report_dir
        path.mkdir(parents=True, exist_ok=True)
        _open_in_file_browser(path)

    @Slot()
    def _on_clear_recent(self) -> None:
        self.ctx.run_state.recent_reports.clear()
        self._refresh_recent_list()


# ===========================================================================
# Module-level helpers (used by both ReportView and ScriptView's auto-save)
# ===========================================================================


def _merge_recent_reports(rs, written: list[Path], cap: int = 20) -> None:
    """Prepend freshly-written reports to ``rs.recent_reports``, dedupe by path,
    and cap the list — the shared tail of both sweep savers."""
    seen: set[str] = set()
    fresh: list[Path] = []
    for path in written + rs.recent_reports:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        fresh.append(path)
    rs.recent_reports = fresh[:cap]


def _save_sweep(
    ctx: AppContext,
    sweep,
    *,
    selected_indexes: list[int] | None = None,
) -> list[Path]:
    """Build a RunReport from a SweepResult and save in the configured formats.

    Updates ``ctx.run_state.recent_reports`` with the produced paths
    (most recent first, capped at 20 entries). Carries the test-record
    metadata from RunState through to the report writers.

    ``selected_indexes`` (Group B) is recorded in the .json sidecar
    so a partial run is auditable.
    """
    rs = ctx.run_state
    started = datetime.fromtimestamp(sweep.started_at)
    basename = render_filename(rs.report_filename_pattern, started)
    # Auto-suffix _2 / _3 / … if the resolved folder already exists.
    # Custom patterns without {date}/{time} tokens (e.g. "test") would
    # otherwise overwrite the previous run silently. The unique name
    # is fed into build_run_report so the report's internal run_id
    # matches the on-disk folder name.
    basename = unique_basename(rs.report_dir, basename)
    report = build_run_report(
        sweep,
        ctx.config,
        run_id=basename,
        metadata=getattr(rs, "report_metadata", None),
        selected_case_indexes=selected_indexes,
        cable_length_m=getattr(rs, "cable_length_m", ""),
    )

    formats: list[ReportFormat] = [
        f for f in rs.report_formats if f in ALL_FORMATS
    ]  # type: ignore[list-item]
    written = save_report(
        report,
        dest_dir=rs.report_dir,
        basename=basename,
        formats=formats,
        also_config=True,
        include_chart_pngs=getattr(rs, "report_include_chart_pngs", True),
    )

    _merge_recent_reports(rs, written)
    return written


def _save_multi_sweep(ctx: AppContext, multi_result) -> list[Path]:
    """Group C-1 counterpart to :func:`_save_sweep`.

    Takes a :class:`MultiSweepResult` (the multi-segment aggregate the
    Client panel builds up after each segment), flattens it into a
    :class:`MultiSegmentRunReport`, and writes the consolidated
    multi-segment report set. The folder name gets a ``_multisegment``
    suffix so flat scans of the Reports/ folder can tell single-sweep
    runs and multi-segment runs apart at a glance.
    """
    rs = ctx.run_state
    started = datetime.fromtimestamp(multi_result.started_at)
    base = render_filename(rs.report_filename_pattern, started)
    basename = f"{base}_multisegment"
    # Auto-suffix _2 / _3 / … if the resolved folder already exists.
    # Same logic as the single-sweep saver — guards against custom
    # patterns without {date}/{time} tokens silently overwriting a
    # previous run.
    basename = unique_basename(rs.report_dir, basename)
    report = build_multi_run_report(
        multi_result,
        ctx.config,
        run_id=basename,
        metadata=getattr(rs, "report_metadata", None),
        cable_length_m=getattr(rs, "cable_length_m", ""),
    )

    formats: list[ReportFormat] = [
        f for f in rs.report_formats if f in ALL_FORMATS
    ]  # type: ignore[list-item]
    written = save_report(
        report,
        dest_dir=rs.report_dir,
        basename=basename,
        formats=formats,
        also_config=True,
        include_chart_pngs=getattr(rs, "report_include_chart_pngs", True),
    )

    _merge_recent_reports(rs, written)
    return written
