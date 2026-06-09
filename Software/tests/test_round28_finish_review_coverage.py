"""Round-28 — coverage closures from the /finish phase review.

The phase-close review (pr-test-analyzer) flagged new logic that shipped this
phase without a direct regression guard. These tests pin the contracts that
matter most — the ones on the 4-second Analysis auto-refresh tick (a malformed
sidecar must never abort it; a re-scan must not re-load duplicates), the
check-state preservation across a runs-list rebuild, the Save-PNG button staying
in sync with the active chart tab, and the global font bump applying even when
the bundled Inter face is unavailable.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from pingpair.config import load_default_config
from pingpair.context import Role

pytest.importorskip("PySide6", reason="Round-28 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="views build pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _analysis(qapp, report_dir: Path):
    from pingpair.context import AppContext, RunState
    from pingpair.views.analysis_view import AnalysisView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round28-analysis"),
        run_state=RunState(role=Role.CLIENT),
    )
    ctx.run_state.report_dir = report_dir
    return AnalysisView(ctx)


def _write_sidecar(root: Path, name: str, *, good: bool = True) -> None:
    """Drop a ``<name>/<name>.json`` sweep sidecar (valid or malformed)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_analysis_loader import _v3_sidecar

    sub = root / name
    sub.mkdir()
    payload = json.dumps(_v3_sidecar(name)) if good else "{ not valid json"
    (sub / f"{name}.json").write_text(payload, encoding="utf-8")


def _loaded_run(path: Path, idx: int):
    from pingpair.analysis import LoadedRun

    return LoadedRun(
        path=path,
        run_id=path.stem,
        display_label=path.stem,
        schema_version=5,
        started_at=datetime(2026, 5, 17, 18, idx % 60),
        duration_s=1.0,
        server_ip="192.168.1.1",
        client_ip="192.168.1.2",
        protocol="udp",
        is_multi_segment=False,
    )


# ---------------------------------------------------------------------------
# gap #1 — a malformed sidecar on the auto-refresh tick is counted + skipped,
# never aborts the scan (the tick fires every 4 s; one bad file must not kill it).
# ---------------------------------------------------------------------------


def test_scan_skips_malformed_sidecar_and_keeps_going(qapp, tmp_path) -> None:
    view = _analysis(qapp, tmp_path)
    _write_sidecar(tmp_path, "PingPair_2026-05-31_010101", good=True)
    _write_sidecar(tmp_path, "PingPair_2026-05-31_020202", good=False)

    added = view._scan_source_folder()

    assert added == 1, "only the good sweep is added; the malformed one is skipped"
    assert len(view._runs) == 1
    assert "couldn't be parsed" in view._status_label.text(), (
        "the malformed file is surfaced in the status line, not swallowed"
    )


# ---------------------------------------------------------------------------
# gap #2 — a re-scan dedups: nothing new returns 0 and does not re-append (else
# the 4 s timer would re-load + re-plot every tick).
# ---------------------------------------------------------------------------


def test_rescan_dedups_and_returns_zero(qapp, tmp_path) -> None:
    view = _analysis(qapp, tmp_path)
    _write_sidecar(tmp_path, "PingPair_2026-05-31_030303", good=True)

    assert view._scan_source_folder() == 1, "first scan loads the sweep"
    assert view._scan_source_folder() == 0, "re-scan finds nothing new"
    assert len(view._runs) == 1, "the sweep is not re-appended on re-scan"
    assert "auto-refreshing" in view._status_label.text()


# ---------------------------------------------------------------------------
# gap #5 — a runs-list rebuild preserves each run's tick state (a sweep the user
# unticked must not silently re-check itself when auto-refresh rebuilds the list).
# ---------------------------------------------------------------------------


def test_rebuild_preserves_check_state(qapp, tmp_path) -> None:
    from PySide6.QtCore import Qt

    view = _analysis(qapp, tmp_path)
    run_a = _loaded_run(tmp_path / "Run_A.json", 0)
    view._runs[:] = [run_a]
    view._rebuild_runs_list()

    # User unticks Run_A.
    view._runs_list.item(0).setCheckState(Qt.CheckState.Unchecked)

    # A new sweep appears and the list is rebuilt (what auto-refresh does).
    view._runs.append(_loaded_run(tmp_path / "Run_B.json", 1))
    view._rebuild_runs_list()

    by_label = {
        view._runs_list.item(r).text(): view._runs_list.item(r).checkState()
        for r in range(view._runs_list.count())
    }
    assert by_label["Run_A"] is Qt.CheckState.Unchecked, "unticked run stays unticked"
    assert by_label["Run_B"] is Qt.CheckState.Checked, "a brand-new run defaults ticked"


# ---------------------------------------------------------------------------
# gap #8 — the left-pane Save-PNG button stays in lock-step with the active
# chart tab's savability (currentChanged → _emit_png_state → _set_save_png_enabled).
# ---------------------------------------------------------------------------


def test_png_button_tracks_active_chart_tab(qapp, tmp_path) -> None:
    view = _analysis(qapp, tmp_path)
    tabs = view._charts._tabs
    assert tabs.count() >= 2, "there are multiple chart tabs to switch between"
    for i in range(tabs.count()):
        tabs.setCurrentIndex(i)
        qapp.processEvents()
        assert view._png_btn.isEnabled() == view._charts.has_savable_chart(), (
            f"Save-PNG button must mirror has_savable_chart() on tab {i}"
        )


# ---------------------------------------------------------------------------
# gap #10 — apply_ui_font bumps the base point size even when the bundled Inter
# face can't be registered (the size bump must not be skipped with the family).
# ---------------------------------------------------------------------------


def test_ui_font_bump_applies_even_without_inter(qapp, monkeypatch) -> None:
    from pingpair import theme

    original = qapp.font()
    try:
        monkeypatch.setattr(theme, "load_ui_font", lambda: None)
        theme.apply_ui_font(qapp)
        assert qapp.font().pointSize() == theme.UI_FONT_POINT_SIZE, (
            "the size bump applies regardless of font-family availability"
        )
    finally:
        qapp.setFont(original)


# ---------------------------------------------------------------------------
# polish — the auto-refresh poll only runs while the Analysis tab is visible
# (started in showEvent / stopped in hideEvent) so it can't walk the reports
# tree every 4s during a sweep on another tab.
# ---------------------------------------------------------------------------


def test_autorefresh_pauses_when_hidden(qapp, tmp_path) -> None:
    view = _analysis(qapp, tmp_path)
    view.show()
    qapp.processEvents()
    assert view._autorefresh_timer.isActive(), "poll runs while the tab is visible"
    view.hide()
    qapp.processEvents()
    assert not view._autorefresh_timer.isActive(), "poll pauses while hidden"
    view.show()
    qapp.processEvents()
    assert view._autorefresh_timer.isActive(), "poll resumes when shown again"


# ---------------------------------------------------------------------------
# polish — the status-line message matrix (empty / new / steady / parse errors).
# ---------------------------------------------------------------------------


def test_update_status_message_matrix(qapp, tmp_path) -> None:
    view = _analysis(qapp, tmp_path)
    view._update_status(0, 0)
    assert "No reports yet" in view._status_label.text()
    view._runs.append(_loaded_run(tmp_path / "R.json", 0))
    view._update_status(1, 0)
    assert "Loaded 1 new" in view._status_label.text()
    view._update_status(0, 0)
    assert "auto-refreshing" in view._status_label.text()
    view._update_status(0, 2)
    assert "couldn't be parsed" in view._status_label.text()


# ---------------------------------------------------------------------------
# polish — the Export button enables only when ≥1 run is ticked (the transition,
# not just the disabled-at-rest state).
# ---------------------------------------------------------------------------


def test_export_button_enables_when_a_run_is_checked(qapp, tmp_path) -> None:
    from PySide6.QtCore import Qt

    view = _analysis(qapp, tmp_path)
    _write_sidecar(tmp_path, "PingPair_2026-05-31_040404", good=True)
    view._scan_source_folder()  # loads + builds the list (item ticked by default)
    view._replot()
    assert view._export_btn.isEnabled(), "a ticked run enables Export"
    view._runs_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    view._replot()
    assert not view._export_btn.isEnabled(), "no ticked runs disables Export again"


# ---------------------------------------------------------------------------
# polish — cancelling the Save-PNG dialog is a clean no-op (no status change,
# no exception).
# ---------------------------------------------------------------------------


def test_save_png_cancel_is_a_noop(qapp, tmp_path, monkeypatch) -> None:
    from pingpair.views import _analysis_charts

    view = _analysis(qapp, tmp_path)
    monkeypatch.setattr(
        _analysis_charts.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: ("", "")),
    )
    view._status_label.setText("SENTINEL")
    view._charts.save_current_png()  # user cancels the dialog
    assert view._status_label.text() == "SENTINEL", "cancel doesn't touch the status"
