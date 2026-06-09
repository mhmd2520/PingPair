"""Round-27 — header version contrast, runs-list checklist, Quick-Start shots.

Reported by Mohamed (img-1..10), quality-first, with an explicit "stop letting
the same small bugs come back" mandate (points 2 / 6). Each fix below ships with
a guarding assertion so a regression fails CI instead of resurfacing on a VM:

* **point 3A** — the version beside the welcome card title was ``palette(mid)``
  grey (barely legible). It now uses the brand accent colour.
* **point 4** — the Loaded-runs list no longer elides run names, so a horizontal
  scrollbar can actually appear; it has its own border + per-row rule so it
  doesn't read as overlapping the group box.
* **point 4A** — the list is ``NoSelection`` / ``NoFocus``: the tick is the only
  state, so a stray row highlight can't desync from the checkbox (the Run-tab
  fix, applied here).
* **point 5** — Help → Quick Start embeds the welcome cards' screenshots (Setup,
  Run), not just their text, by resolving ``<tab>/<file>`` against the running
  role's ``_shots`` root.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import pytest

from pingpair.config import load_default_config
from pingpair.context import Role

pytest.importorskip("PySide6", reason="Round-27 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="views build pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ===========================================================================
# point 3A — welcome header version is high-contrast, not palette(mid) grey.
# ===========================================================================


def test_welcome_header_version_is_bright(qapp) -> None:
    from pingpair import __version__, theme
    from pingpair.views.welcome import WelcomeDialog

    dlg = WelcomeDialog(dark=True, role="client")
    style = dlg._hdr_version.styleSheet()
    assert "palette(mid)" not in style, "the version is no longer low-contrast grey"
    accent = theme.PALETTES["dark"]["accent"]
    assert accent.lower() in style.lower(), "the version uses the brand accent colour"
    assert dlg._hdr_version.text() == f"v{__version__}"


def test_card_without_image_drops_the_click_hint(qapp) -> None:
    """3B — a card whose screenshot is absent must not dangle a 'click the image
    to enlarge' hint with nothing to click (the finish-popup card-5 case)."""
    from pingpair.views.welcome import WelcomeDialog
    from pingpair.welcome_cards import Card

    dlg = WelcomeDialog(dark=True, role="client")
    missing = Card(
        title="x",
        body_html="<p>body</p><p class='hint'>This is the popup — click the image.</p>",
        image="save-options/does-not-exist.png",
    )
    html = dlg._card_html(missing)
    assert "click the image" not in html.lower(), "no dangling click hint when no image"
    assert "<img" not in html, "no broken image either"
    assert "body" in html, "the real content survives"

    # A card WITH a resolvable image keeps its figure + the 'Click to enlarge' cap.
    from pingpair.welcome_cards import QUICK_START_CARDS

    with_image = dlg._card_html(QUICK_START_CARDS[0])  # topology
    assert "<img" in with_image and "Click the image to enlarge" in with_image


# ===========================================================================
# points 4 / 4A / 4B — the Loaded-runs list is a no-select checklist.
# ===========================================================================


def _analysis(qapp):
    from pingpair.context import AppContext, RunState
    from pingpair.views.analysis_view import AnalysisView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round27-analysis"),
        run_state=RunState(role=Role.CLIENT),
    )
    ctx.run_state.report_dir = Path(tempfile.gettempdir())
    return AnalysisView(ctx)


def test_runs_list_shows_name_with_details_in_tooltip(qapp) -> None:
    """2D — FINAL (2026-05-31): no scrollbar, no wrap. Each row shows the run's
    NAME on one clean single line (elided with "…" if very long); the verbose
    summary + path live in the tooltip. No horizontal scrollbar; no widget
    stylesheet (any QSS shadows the theme); names never wrap into a mess."""
    from datetime import datetime

    from PySide6.QtCore import Qt

    from pingpair.analysis import LoadedRun

    view = _analysis(qapp)
    assert view._runs_list.styleSheet() == "", "no widget stylesheet"
    assert view._runs_list.wordWrap() is False, "names do NOT wrap (2D)"
    assert (
        view._runs_list.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    ), "no horizontal scrollbar"
    assert view._runs_list.textElideMode() == Qt.TextElideMode.ElideRight

    # The item text is the run NAME; the tooltip carries the full summary.
    from pathlib import Path

    run = LoadedRun(
        path=Path("x/PingTool_2026-05-17_1801_multisegment.json"),
        run_id="PingTool_2026-05-17_1801_multisegment",
        display_label="PingTool_2026-05-17_1801_multisegment",
        schema_version=5, started_at=datetime(2026, 5, 17, 18, 1),
        duration_s=1.0, server_ip="192.168.1.1", client_ip="192.168.1.2",
        protocol="udp", is_multi_segment=True,
    )
    view._runs[:] = [run]
    view._rebuild_runs_list()
    item = view._runs_list.item(0)
    assert item.text() == "PingTool_2026-05-17_1801_multisegment", "row shows the name"
    assert run.summary_line() in item.toolTip(), "tooltip carries the full summary"


def test_runs_list_is_no_frame_and_fixed_size(qapp) -> None:
    """2026-05-31 — no box-inside-box (the list's own frame is removed so only the
    group box frames the rows), and the list is a FIXED size: it reserves room
    for _RUNS_VISIBLE_ROWS rows no matter how many runs are loaded (empty rows =
    breathing room; beyond that it scrolls). The block must be the SAME height
    whether the list holds 0 runs or many (no jump when the first run loads)."""
    from datetime import datetime
    from pathlib import Path

    from PySide6.QtWidgets import QFrame

    from pingpair.analysis import LoadedRun
    from pingpair.views.analysis_view import AnalysisView

    view = _analysis(qapp)
    assert view._runs_list.frameShape() == QFrame.Shape.NoFrame, "no box-inside-box"
    empty_h = view._runs_list.height()

    def _runs(n):
        return [
            LoadedRun(
                path=Path(f"x/Run_{i}.json"), run_id=f"Run_{i}",
                display_label=f"Run_{i}", schema_version=5,
                started_at=datetime(2026, 5, 17, 18, i % 60), duration_s=1.0,
                server_ip="1", client_ip="2", protocol="udp", is_multi_segment=False,
            )
            for i in range(n)
        ]

    view._runs[:] = _runs(3)
    view._rebuild_runs_list()
    few_h = view._runs_list.height()
    view._runs[:] = _runs(25)
    view._rebuild_runs_list()
    many_h = view._runs_list.height()

    # Same fixed block whether empty, a few, or overflowing.
    assert empty_h == few_h == many_h, "the runs block is a fixed size"
    row_h = view._runs_list.sizeHintForRow(0)
    assert few_h >= AnalysisView._RUNS_VISIBLE_ROWS * row_h, "reserves N rows"
    # 25 runs overflow the fixed block → it scrolls (needs a laid-out viewport).
    view.resize(1100, 760)
    view.show()
    qapp.processEvents()
    assert view._runs_list.verticalScrollBar().maximum() > 0, "scrolls when full"


def test_runs_list_has_no_selection_highlight(qapp) -> None:
    """2B — the row-selection highlight is removed via the NoSelection PROPERTY
    (a property, not a stylesheet, so it can't break the scrollbar)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView

    view = _analysis(qapp)
    assert (
        view._runs_list.selectionMode()
        == QAbstractItemView.SelectionMode.NoSelection
    ), "no row-selection highlight (2B)"
    assert view._runs_list.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_tick_replot_is_debounced(qapp) -> None:
    """2C — ticking debounces the (expensive) chart replot so it isn't laggy."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem

    view = _analysis(qapp)
    assert hasattr(view, "_replot_timer") and view._replot_timer.isSingleShot()
    item = QListWidgetItem("run-x")
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(Qt.CheckState.Unchecked)
    view._runs_list.addItem(item)
    item.setCheckState(Qt.CheckState.Checked)  # fires itemChanged
    assert view._replot_timer.isActive(), "a tick arms the debounce, not a sync replot"


def test_source_section_removed_and_list_auto_refreshes(qapp, tmp_path) -> None:
    """2026-05-31 — the Source section (folder field + Browse / Refresh / Add
    file…) is gone; the runs list auto-refreshes from the Save Options
    destination so newly-saved sweeps appear on their own, and the status line is
    concise (no long folder path that would wrap into the rows)."""
    import json

    from PySide6.QtWidgets import QGroupBox, QPushButton

    from pingpair.context import AppContext, RunState
    from pingpair.views.analysis_view import AnalysisView

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_analysis_loader import _v3_sidecar

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round27-autorefresh"),
        run_state=RunState(role=Role.CLIENT),
    )
    ctx.run_state.report_dir = tmp_path
    view = AnalysisView(ctx)

    titles = {b.title() for b in view.findChildren(QGroupBox)}
    assert "Source" not in titles, "the Source group box is gone"
    labels = {b.text() for b in view.findChildren(QPushButton)}
    assert labels.isdisjoint({"Browse…", "Refresh", "Add file…"}), "source buttons gone"
    assert view._autorefresh_timer.interval() == 4000
    # The poll only runs while the tab is visible (started in showEvent), so it
    # can't compete with report writes during a sweep on another tab.
    view.show()
    qapp.processEvents()
    assert view._autorefresh_timer.isActive()
    assert len(view._runs) == 0

    # A new sweep appears in the folder → auto-refresh picks it up.
    name = "PingTool_2026-05-31_010101"
    sub = tmp_path / name
    sub.mkdir()
    (sub / f"{name}.json").write_text(json.dumps(_v3_sidecar(name)), encoding="utf-8")
    view._auto_refresh()
    assert len(view._runs) == 1, "auto-refresh loaded the new sweep"
    assert "Software" not in view._status_label.text(), "status has no long folder path"


def test_export_and_png_buttons_moved_to_left_pane(qapp, tmp_path) -> None:
    """2026-05-31 — the Export / Save-chart buttons moved from the chart toolbar
    to the left pane (under Filters), with concise labels. The chart panel no
    longer owns the buttons; it exposes save_current_png() + a png-state callback
    so the left-pane Save button enables only on a savable chart tab."""
    from pingpair.context import AppContext, RunState
    from pingpair.views.analysis_view import AnalysisView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round27-buttons"),
        run_state=RunState(role=Role.CLIENT),
    )
    ctx.run_state.report_dir = tmp_path
    view = AnalysisView(ctx)

    # Buttons live on the view (left pane), with the concise labels.
    assert view._export_btn.text() == "Export report…"
    assert view._png_btn.text() == "Save chart PNG…"
    # Export is disabled until a run is ticked (no runs loaded here).
    assert not view._export_btn.isEnabled()
    # The chart panel exposes the public API and no longer has its own buttons.
    assert hasattr(view._charts, "save_current_png")
    assert hasattr(view._charts, "has_savable_chart")
    assert not hasattr(view._charts, "_export_btn")
    assert not hasattr(view._charts, "_png_btn")
    # Save-PNG button tracks the active chart tab: a plot tab → enabled.
    assert view._charts.has_savable_chart() == view._png_btn.isEnabled()


# ===========================================================================
# point 5 — Help Quick Start embeds the welcome screenshots, not just text.
# ===========================================================================


def _help(qapp):
    from pingpair.context import AppContext, RunState
    from pingpair.views.help_view import HelpView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round27-help"),
        run_state=RunState(role=Role.CLIENT),
    )
    return HelpView(ctx)


def test_help_resolves_cross_tab_shots_in_quick_start(qapp) -> None:
    """A ``setup/...`` / ``run/...`` reference resolves against the role's shots
    root while the Quick Start section is current (point 5)."""
    view = _help(qapp)
    view.open_section("quick-start")
    qapp.processEvents()
    for name in ("setup/01-checks-overview.png", "run/01-overview.png"):
        path = view._resolve_shot_path(name)
        assert path is not None and path.is_file(), f"Quick Start should resolve {name}"


def test_quick_start_embeds_screenshots_not_just_text(qapp) -> None:
    """The rendered Quick Start page carries real (scaled) screenshot resources,
    not only the diagram — i.e. more than one embedded image (point 5)."""
    view = _help(qapp)
    view.resize(1300, 900)
    view.open_section("quick-start")
    qapp.processEvents()
    html = view._browser.toHtml()
    assert html.count("mem://help/") >= 3, (
        "Quick Start should embed the topology + Setup + Run shots, "
        f"got {html.count('mem://help/')} embedded images"
    )


def test_committed_quick_start_references_the_screenshots() -> None:
    """The committed HTML (pure, no Qt) carries the per-role figure refs.

    2026-06-02: figures are now role-qualified (``<role>/<tab>/<file>``) so
    Quick Start mirrors the welcome tour per-role instead of following the
    running role; the finish popup was captured, so the Save step now embeds it.
    """
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR

    by_key = {s.key: s for s in list_sections(HELP_DIR)}
    qs = by_key["quick-start"].index_path.read_text(encoding="utf-8")
    # Setup steps pinned to each role; Run + Save pinned to Client.
    assert 'src="server/setup/01-checks-overview.png"' in qs
    assert 'src="client/setup/01-checks-overview.png"' in qs
    assert 'src="loopback/setup/01-checks-overview.png"' in qs
    assert 'src="client/run/01-overview.png"' in qs
    # The finish popup is now captured (Client) and embedded on the Save step.
    assert 'src="client/save-options/02-finish-popup.png"' in qs


def test_quick_start_matches_generator_after_screenshot_change() -> None:
    """Guards drift: the committed file must still equal the generator output
    now that the generator emits figures (point 5)."""
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools_dir))
    import build_quick_start_help as gen

    assert gen.OUT_PATH.read_text(encoding="utf-8") == gen.render_quick_start_html(), (
        "Quick Start HTML is stale — run tools/build_quick_start_help.py"
    )
