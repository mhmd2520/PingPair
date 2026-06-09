"""Analysis tab — overlay & compare past sweeps (Group C-2).

Top-level orchestrator. Heavy lifting lives in sibling modules:

* :mod:`_analysis_filters` — Filters group box + predicates.
* :mod:`_analysis_charts` — chart sub-tabs (overlay + Stats + Trend +
  Diff). The Export / Save-chart-PNG buttons live in this module's left
  pane, not in the charts widget.
* :mod:`_export_comparison_dialog` — modal for the Export button (#12).

This module owns the runs list (auto-refreshed from the Save Options
destination) and the top-level export orchestration (rasterising charts → building a
:class:`pingpair.analysis.ComparisonReport` → calling
:func:`pingpair.reporting.save_comparison_report`).
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..analysis import (
    LoadedRun,
    SidecarParseError,
    build_comparison_report,
    enumerate_sidecars,
    load_sidecar,
)
from ..context import AppContext
from ..reporting import save_comparison_report, unique_basename
from ._analysis_charts import PALETTE, AnalysisCharts, export_plot_png
from ._analysis_filters import AnalysisFilters
from ._base import BaseView
from ._export_comparison_dialog import ExportComparisonDialog


class AnalysisView(BaseView):
    """The Analysis tab — overlay one or more past sweeps."""

    title = "Analysis — overlay & compare runs"

    # The runs list reserves a FIXED block this many rows tall (empty rows show as
    # breathing room when fewer runs are loaded; it scrolls beyond this). 13 rows:
    # the Source removal freed ~5 rows over the old 10, of which a couple were
    # handed back to make room for the Export / Save-chart buttons below Filters.
    _RUNS_VISIBLE_ROWS = 13

    def __init__(self, ctx: AppContext) -> None:
        self._runs: list[LoadedRun] = []
        super().__init__(ctx)

    def _build_placeholder(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        outer.addWidget(QLabel(f"<h2>{self.title}</h2>"))
        # One concise line (the old three-sentence blurb trimmed off-screen);
        # word-wrap is on as a safety so a narrow window wraps instead of cutting.
        intro = QLabel(
            "Tick past sweeps on the left to overlay their per-case metrics; "
            "new sweeps from your Save Options folder appear automatically."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # Left pane sized so the two side-by-side buttons under Filters fit and
        # align with the boxes above, while keeping the chart as wide as possible.
        splitter.setSizes([405, 760])
        outer.addWidget(splitter, stretch=1)

        self._scan_source_folder()
        self._replot()

    def _build_left_pane(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)

        # The "Source" section (folder field + Browse / Refresh / Add file…) was
        # removed 2026-05-31: the source folder is always the Save Options
        # destination, the list auto-refreshes for new sweeps (see the timer +
        # _auto_refresh below), and the freed space is given to the runs box.

        # Loaded runs list
        runs_box = QGroupBox("Loaded runs (tick to plot)")
        runs_layout = QVBoxLayout(runs_box)
        runs_layout.setContentsMargins(12, 10, 12, 12)
        self._runs_list = QListWidget()
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(11)
        self._runs_list.setFont(mono)
        # The runs list carries NO setStyleSheet — any QSS on a QListWidget makes
        # Qt drop the theme's QScrollBar style; everything below is done via
        # PROPERTIES.
        #   * 2B — remove the row-selection HIGHLIGHT (NoSelection); the tick is
        #     the only state, and the checkbox indicator still toggles.
        self._runs_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._runs_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 2D (final, 2026-05-31) — the horizontal scrollbar would not reliably
        # paint on the user's Windows, and word-wrapping the long underscore-only
        # filenames was a mess (no spaces to break on → clipped + overlapping
        # rows). Clean fix: each row shows the run's NAME on ONE line (the name is
        # the long part; the verbose "· date · n/n ok · tag" suffix moved to the
        # tooltip — see _rebuild_runs_list). A name that's still too wide elides
        # with "…"; the full name + details are always one hover away. No
        # horizontal scrollbar to depend on, no wrapping mess.
        self._runs_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._runs_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # No "box-inside-box": drop the list's own frame so only the group box
        # frames the rows (a property — doesn't touch the scrollbar).
        self._runs_list.setFrameShape(QFrame.Shape.NoFrame)
        self._runs_list.itemChanged.connect(self._on_item_changed)
        runs_layout.addWidget(self._runs_list, stretch=0)
        # A generous gap pushes the status DOWN to the bottom of the box, well
        # clear of the run names — the box had unused space at the end the status
        # wasn't using (the screenshot showed it floating mid-box).
        runs_layout.addSpacing(36)
        # Status line — ISOLATED below the list, one concise line at the bottom.
        # word-wrap stays ON (a non-wrapping label demands its full text width as
        # its minimum, which would force the whole left pane wider — point 2); the
        # messages are short + path-free so they never actually wrap at this width.
        self._status_label = QLabel("")
        status_font = QFont()
        status_font.setPointSize(10)
        self._status_label.setFont(status_font)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color:#888;")
        self._status_label.setFixedHeight(
            self._status_label.fontMetrics().height() + 4
        )
        runs_layout.addWidget(self._status_label, stretch=0)
        # The list is a FIXED block (_adjust_runs_list_height) — _RUNS_VISIBLE_ROWS
        # rows regardless of the run count: a few runs leave empty rows for
        # breathing room; >N scrolls (mouse wheel; vertical scrollbar). The box
        # doesn't stretch — the spare PANE height drops to the trailing stretch.
        layout.addWidget(runs_box, stretch=0)
        self._adjust_runs_list_height()

        # 2C — debounce the replot so ticking isn't laggy (a full chart replot on
        # every tick blocked the UI thread). A 150 ms single-shot timer coalesces
        # rapid ticks; the checkbox repaints instantly, the replot lands a moment
        # later.
        self._replot_timer = QTimer(self)
        self._replot_timer.setSingleShot(True)
        self._replot_timer.setInterval(150)
        self._replot_timer.timeout.connect(self._replot)

        # Filters
        self._filters = AnalysisFilters()
        self._filters.filters_changed.connect(self._on_filter_changed)
        layout.addWidget(self._filters, stretch=0)

        # Export / Save-chart buttons — moved here from the chart toolbar so they
        # sit under Filters, side by side, aligned to the same left-pane borders
        # as the boxes above. Concise labels so both fit.
        btn_row = QHBoxLayout()
        self._export_btn = QPushButton("Export report…")
        self._export_btn.setToolTip(
            "Export the ticked runs as a comparison report (docx / xlsx / pdf / "
            "txt). The active filter is honoured. Enabled once a run is ticked."
        )
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export_comparison)
        btn_row.addWidget(self._export_btn, stretch=1)
        self._png_btn = QPushButton("Save chart PNG…")
        self._png_btn.setToolTip(
            "Save the chart currently shown on the right as a PNG. Enabled while "
            "a chart sub-tab (not Stats / Diff) is active."
        )
        self._png_btn.clicked.connect(lambda: self._charts.save_current_png())
        btn_row.addWidget(self._png_btn, stretch=1)
        layout.addLayout(btn_row)
        layout.addStretch(1)

        # Auto-refresh: poll the source folder so newly-saved sweeps appear in the
        # list on their own (no Refresh button). Cheap — enumerate + dedup; only
        # replots when something new actually loads. Started in showEvent /
        # stopped in hideEvent so the folder walk only runs while the Analysis
        # tab is visible — no background I/O competing with report writes during
        # a sweep on another tab. (refresh() re-scans on tab activation, so a
        # paused-while-hidden timer loses nothing on the way back.)
        self._autorefresh_timer = QTimer(self)
        self._autorefresh_timer.setInterval(4000)
        self._autorefresh_timer.timeout.connect(self._auto_refresh)

        return wrapper

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._autorefresh_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self._autorefresh_timer.stop()

    def _auto_refresh(self) -> None:
        """Poll the source folder so newly-saved sweeps load on their own."""
        if self._scan_source_folder():
            self._replot()

    def _adjust_runs_list_height(self) -> None:
        """Give the runs list a FIXED height — room for ``_RUNS_VISIBLE_ROWS``
        rows regardless of the run count.

        A new/empty list still reserves the full block (empty rows = breathing
        room); >N runs scroll (mouse wheel; vertical scrollbar via Qt's
        AsNeeded default — the horizontal policy is the only one pinned). Row
        height comes from a throwaway probe item when the list is empty so the
        block is the same size whether empty or full (no jump on first load).
        Pure layout — no scrollbar/native dependency.
        """
        row_h = self._runs_list.sizeHintForRow(0) if self._runs_list.count() else 0
        if row_h <= 0:
            blocked = self._runs_list.blockSignals(True)
            probe = QListWidgetItem("Mg")
            self._runs_list.addItem(probe)
            row_h = self._runs_list.sizeHintForRow(0)
            self._runs_list.takeItem(self._runs_list.row(probe))
            self._runs_list.blockSignals(blocked)
        if row_h <= 0:
            row_h = self._runs_list.fontMetrics().height()
        self._runs_list.setFixedHeight(self._RUNS_VISIBLE_ROWS * row_h + 8)

    def _build_right_pane(self) -> QWidget:
        self._charts = AnalysisCharts(
            run_filter=self._filters.run_passes_metadata,
            case_filter=self._filters.case_passes,
            get_source_dir=self._current_source_root,
            status_callback=lambda msg: self._status_label.setText(msg),
            on_png_state_changed=self._set_save_png_enabled,
        )
        return self._charts

    def _set_save_png_enabled(self, enabled: bool) -> None:
        """Enable the left-pane Save-chart button only on a savable chart tab."""
        if hasattr(self, "_png_btn"):
            self._png_btn.setEnabled(enabled)

    def refresh(self) -> None:
        # Re-scan when the tab is shown — new sweeps may have been saved while we
        # were on another tab.
        if self._scan_source_folder():
            self._replot()

    def _current_source_root(self) -> Path:
        # The source folder is always the Save Options destination now (the
        # editable Source field was removed).
        return self.ctx.run_state.report_dir

    def _scan_source_folder(self) -> int:
        """Load any newly-appeared sidecars from the source folder.

        Returns the number of runs added (0 if nothing new) so callers (the
        auto-refresh timer, tab activation) only replot when something changed.
        Status text is concise and path-free so it can't wrap into the rows.
        """
        root = self._current_source_root()
        sidecars = enumerate_sidecars(root)
        already_loaded = {r.path.resolve() for r in self._runs}
        new_paths = [p for p in sidecars if p.resolve() not in already_loaded]
        added = 0
        errors = 0
        for path in new_paths:
            try:
                run = load_sidecar(path)
            except SidecarParseError as exc:
                errors += 1
                self.ctx.logger.warning(
                    "skipped malformed sidecar %s: %s", path, exc
                )
                continue
            self._runs.append(run)
            added += 1
        if added or errors:
            self._runs.sort(
                key=lambda r: (r.started_at or datetime.min),
                reverse=True,
            )
            self._rebuild_runs_list()
        self._update_status(added, errors)
        return added

    def _update_status(self, added: int, errors: int) -> None:
        """Concise, path-free status under the list (so it can't overlap rows)."""
        total = len(self._runs)
        if errors:
            self._status_label.setText(
                f"{total} run(s) loaded · {errors} couldn't be parsed (see log)."
            )
        elif total == 0:
            self._status_label.setText("No reports yet — they appear automatically.")
        elif added:
            self._status_label.setText(f"Loaded {added} new · {total} run(s) total.")
        else:
            self._status_label.setText(f"{total} run(s) loaded · auto-refreshing.")

    def _rebuild_runs_list(self) -> None:
        prev_checked: dict[str, bool] = {}
        for row in range(self._runs_list.count()):
            item = self._runs_list.item(row)
            if item is None:
                continue
            idx = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(idx, int) and 0 <= idx < len(self._runs):
                prev_checked[str(self._runs[idx].path.resolve())] = (
                    item.checkState() is Qt.CheckState.Checked
                )
        self._runs_list.blockSignals(True)
        try:
            self._runs_list.clear()
            for idx, run in enumerate(self._runs):
                # Show the run NAME on one line (it fits); the verbose summary
                # (date · n/n ok · tag) + the source path go in the tooltip so
                # hovering reveals everything without needing a scrollbar.
                item = QListWidgetItem(run.display_label)
                item.setToolTip(f"{run.summary_line()}\n{run.path}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                was_checked = prev_checked.get(str(run.path.resolve()), True)
                item.setCheckState(
                    Qt.CheckState.Checked if was_checked
                    else Qt.CheckState.Unchecked
                )
                item.setData(Qt.ItemDataRole.UserRole, idx)
                if self._filters.run_passes_metadata(run):
                    colour = PALETTE[idx % len(PALETTE)]
                    item.setForeground(QColor(colour))
                else:
                    item.setForeground(QColor("#555555"))
                self._runs_list.addItem(item)
        finally:
            self._runs_list.blockSignals(False)

    def _on_item_changed(self, _item: QListWidgetItem) -> None:
        # 2C: debounced so the tick stays snappy; coalesce rapid toggles.
        self._replot_timer.start()

    def _on_filter_changed(self) -> None:
        if not hasattr(self, "_runs_list"):
            return
        self._rebuild_runs_list()
        self._replot()

    def _checked_runs(self) -> list[tuple[int, LoadedRun]]:
        out: list[tuple[int, LoadedRun]] = []
        for row in range(self._runs_list.count()):
            item = self._runs_list.item(row)
            if item is None or item.checkState() is not Qt.CheckState.Checked:
                continue
            idx = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(idx, int) and 0 <= idx < len(self._runs):
                out.append((idx, self._runs[idx]))
        return out

    def _replot(self) -> None:
        if hasattr(self, "_charts"):
            checked = self._checked_runs()
            self._charts.replot(checked)
            self._export_btn.setEnabled(len(checked) >= 1)

    def _rasterise_charts(self, target_dir: Path) -> dict[str, Path]:
        """Export the four metric PlotWidgets to PNG; return code → path.

        Writes one ``<code>.png`` per metric into ``target_dir`` (a scratch
        folder); :func:`save_comparison_report` then relocates them into the
        per-sweep ``Analysis_Images/`` subfolder so the export matches a
        normal sweep's layout. Returns an empty dict (reports just skip
        charts) when pyqtgraph's exporter is unavailable; a single chart
        that fails to rasterise is logged and skipped without losing the
        others.
        """
        chart_pngs: dict[str, Path] = {}
        for code in ("thr", "lat", "loss", "jit"):
            pw = self._charts.metric_plot_widget(code)
            if pw is None:
                self.ctx.logger.info(
                    "no PlotWidget for metric code %r - skipping", code
                )
                continue
            png_path = target_dir / f"{code}.png"
            try:
                export_plot_png(pw.plotItem, png_path)
            except ImportError as exc:
                self.ctx.logger.warning(
                    "pyqtgraph.exporters unavailable - reports will skip "
                    "charts: %s", exc,
                )
                break  # no exporter at all — don't retry the remaining metrics
            except Exception as exc:
                self.ctx.logger.warning(
                    "could not rasterise %s chart: %s", code, exc
                )
                continue
            chart_pngs[code] = png_path
            self.ctx.logger.info("exported %s chart -> %s", code, png_path)
        return chart_pngs

    @Slot()
    def _on_export_comparison(self) -> None:
        """Open the Export dialog and dispatch to save_comparison_report."""
        checked = self._checked_runs()
        if not checked:
            self._status_label.setText(
                "Tick at least one run on the left before exporting."
            )
            return
        runs = [run for _idx, run in checked]
        default_dir = self.ctx.run_state.report_dir
        default_basename = unique_basename(
            default_dir,
            f"Comparison_{datetime.now().strftime('%Y-%m-%d_%H%M')}",
        )
        dlg = ExportComparisonDialog(
            default_dir=default_dir,
            default_basename=default_basename,
            n_runs=len(runs),
            parent=self,
        )
        # PySide6 6.11: use the class-level QDialog.Accepted constant
        # (== 1) instead of dlg.DialogCode.Accepted. Instance-level nested
        # enum access has been flaky across Qt versions and silently
        # produces False when accidentally comparing against an enum
        # member instead of its int value.
        from PySide6.QtWidgets import QDialog as _QDialog
        result_code = dlg.exec()
        if result_code != _QDialog.DialogCode.Accepted:
            self.ctx.logger.info(
                "Export comparison report cancelled (exec=%s)", result_code
            )
            return
        choice = dlg.result_value()
        if choice is None:
            QMessageBox.warning(
                self, "Export comparison report",
                "Basename and at least one output format are required.",
            )
            return
        try:
            choice.destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self, "Export failed",
                f"Could not create destination folder:\n\n{exc}",
            )
            return
        dest_dir = choice.destination_dir
        basename = choice.basename
        formats = list(choice.formats)
        # Rasterise the live pyqtgraph charts on the GUI thread into a
        # scratch folder; save_comparison_report relocates them into the
        # per-sweep Analysis_Images/ subfolder so the export lands in a
        # SINGLE folder shaped like a normal sweep (report files at the top
        # level + an Analysis_Images/ subfolder of charts) instead of the
        # old two-folder split. The report object is pure data, so the
        # (multi-format, multi-second) write is offloaded off the GUI thread
        # to avoid freezing the window (H4, 2026-06-04); run_save_in_background
        # blocks until it finishes, so the scratch dir stays alive for the copy.
        from ._qt_runner import run_save_in_background

        with tempfile.TemporaryDirectory(prefix="pingpair_cmp_charts_") as tmp:
            chart_pngs = self._rasterise_charts(Path(tmp))
            report = build_comparison_report(
                runs=runs,
                case_filter=self._filters.case_passes,
                filter_description=self._filters.filter_description(),
                chart_pngs=chart_pngs,
                notes=choice.notes,
            )
            self.ctx.logger.info(
                "Calling save_comparison_report dest=%s basename=%s formats=%s",
                dest_dir, basename, formats,
            )
            written, err = run_save_in_background(
                self,
                lambda: save_comparison_report(
                    report, dest_dir, basename, formats
                ),
                logger=self.ctx.logger,
            )
        if err:
            self.ctx.logger.error("save_comparison_report failed: %s", err)
            QMessageBox.critical(
                self, "Export failed",
                f"Could not save comparison report:\n\n{err}\n\n"
                "Full traceback in the application log "
                "(usually under %APPDATA%/PingPair/logs/).",
            )
            return
        # save_comparison_report may bump the basename on a folder clash, so
        # trust the returned paths for the real output folder. Report files
        # come first in the list, so written[0].parent is the sweep folder.
        comparison_dir = written[0].parent if written else dest_dir / basename
        # No success pop-up (user feedback 2026-06-07) — the status line below
        # is enough; the popup was an extra click after every export.
        self.ctx.logger.info(
            "Saved comparison report (%d files) to %s",
            len(written), comparison_dir,
        )
        self._status_label.setText(
            f"Saved comparison report ({len(written)} files) → {comparison_dir}"
        )
