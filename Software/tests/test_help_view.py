"""Qt-widget smoke tests for the Feature-8 Help tab.

Builds a real :class:`HelpView` under the offscreen Qt platform (forced by
``conftest.py``) against the actual shipped help sections, and checks the
behaviours the kickoff + VM review call out: the sidebar lists every section,
selecting a section swaps the rendered content, the injected theme CSS differs
between Light and Dark, and the guide-wide search drives Prev / Next across
sections.
"""

from __future__ import annotations

import logging

import pytest

from pingpair.config import load_default_config
from pingpair.context import AppContext, RunState
from pingpair.help_loader import list_sections
from pingpair.paths import HELP_DIR


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="HelpView is a Qt widget")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _build_help_view(qapp):
    from pingpair.views.help_view import HelpView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-help"),
        run_state=RunState(),
    )
    return HelpView(ctx)


def test_sidebar_lists_every_section(qapp) -> None:
    expected = list_sections(HELP_DIR)
    assert expected, "shipped help sections should be discoverable"

    view = _build_help_view(qapp)

    assert view._sidebar.count() == len(expected)
    # Sidebar labels come from each section's <title>, in NN order.
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]
    assert labels == [s.title for s in expected]


def test_sidebar_labels_match_tab_names(qapp) -> None:
    # Per the VM review: the per-tab guides must carry the bare tab name (no
    # numeric prefix, no leaked HTML entity), and the extra references stay
    # descriptive.
    view = _build_help_view(qapp)
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]

    for tab in ("Setup", "Ping", "Config", "Run", "Save Options", "Analysis"):
        assert tab in labels, f"{tab} guide should be labelled exactly '{tab}'"
    assert not any("&amp;" in label for label in labels), "entities must be decoded"
    assert not any(label[:1].isdigit() for label in labels), "no numeric prefixes"
    assert "fping reference" in labels
    assert "iperf3 reference" in labels


def test_selecting_a_section_swaps_content(qapp) -> None:
    view = _build_help_view(qapp)
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]

    view._sidebar.setCurrentRow(labels.index("Setup"))
    setup_text = view._browser.toPlainText()
    view._sidebar.setCurrentRow(labels.index("Config"))
    config_text = view._browser.toPlainText()

    assert setup_text and config_text
    assert setup_text != config_text
    assert "prerequisite" in setup_text.lower()
    assert "test plan" in config_text.lower()


def test_theme_css_differs_between_light_and_dark(qapp) -> None:
    from PySide6.QtGui import QColor, QPalette

    view = _build_help_view(qapp)

    def _css_for(dark: bool) -> str:
        pal = view.palette()
        pal.setColor(
            QPalette.ColorRole.Window,
            QColor("#0b1220") if dark else QColor("#ffffff"),
        )
        view.setPalette(pal)
        return view._theme_css()

    dark_css = _css_for(dark=True)
    light_css = _css_for(dark=False)

    assert dark_css != light_css
    # Dark uses the bright cyan accent; light uses the deeper teal.
    assert "#22d3ee" in dark_css
    assert "#0891b2" in light_css


# ----- guide-wide search -----------------------------------------------


def test_prev_next_disabled_until_search(qapp) -> None:
    view = _build_help_view(qapp)
    # Fresh view: match-nav buttons are inert until a search finds something.
    assert not view._prev_btn.isEnabled()
    assert not view._next_btn.isEnabled()
    assert view._results.text() == ""


def test_find_returns_without_error_on_empty_query(qapp) -> None:
    # Guard: an empty search box must be a no-op, not a crash.
    view = _build_help_view(qapp)
    view._sidebar.setCurrentRow(0)
    view._search.setText("")
    view._on_find()  # should simply reset + return
    assert not view._next_btn.isEnabled()


def test_global_search_enables_match_nav(qapp) -> None:
    view = _build_help_view(qapp)
    # "PingPair" appears across many sections.
    view._search.setText("PingPair")
    view._on_find()

    assert view._prev_btn.isEnabled()
    assert view._next_btn.isEnabled()
    assert view._total_matches >= 1
    assert view._match_pos == 1
    assert view._results.text() == f"1 / {view._total_matches}"


def test_search_jumps_to_a_matching_section(qapp) -> None:
    # A term that lives on the iperf3 reference page should land on a section
    # that actually contains it (not necessarily the first section).
    view = _build_help_view(qapp)
    view._search.setText("iperf3")
    view._on_find()

    assert view._match_sections, "iperf3 should be found somewhere"
    assert view._current in view._match_sections
    assert "iperf3" in view._browser.toPlainText().lower()


def test_next_advances_and_wraps_the_counter(qapp) -> None:
    view = _build_help_view(qapp)
    view._search.setText("PingPair")
    view._on_find()
    total = view._total_matches
    assert total >= 2, "need several matches to exercise wrap"

    view._go_next()
    assert view._match_pos == 2

    # Walk to the end and one past it -> wraps back to 1.
    for _ in range(total - 2):
        view._go_next()
    assert view._match_pos == total
    view._go_next()
    assert view._match_pos == 1

    # Prev from the first match wraps to the last.
    view._go_prev()
    assert view._match_pos == total


def test_clearing_search_disables_match_nav(qapp) -> None:
    view = _build_help_view(qapp)
    view._search.setText("PingPair")
    view._on_find()
    assert view._next_btn.isEnabled()

    view._search.setText("")  # textChanged -> _on_search_text_changed -> reset
    assert not view._prev_btn.isEnabled()
    assert not view._next_btn.isEnabled()
    assert view._results.text() == ""


# ----- cross-links + highlight-all -------------------------------------


def test_help_link_jumps_to_section(qapp) -> None:
    from PySide6.QtCore import QUrl

    view = _build_help_view(qapp)
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]

    # An in-guide href="help:troubleshooting" should select that section.
    view._on_anchor_clicked(QUrl("help:troubleshooting"))
    assert labels[view._current] == "Troubleshooting"

    view._on_anchor_clicked(QUrl("help:fping-reference"))
    assert labels[view._current] == "fping reference"

    # An unknown key is a safe no-op (stays put), not a crash.
    before = view._current
    view._on_anchor_clicked(QUrl("help:does-not-exist"))
    assert view._current == before


def test_open_section_routes_by_key(qapp) -> None:
    # The cross-tab entry point (MainWindow.open_help -> open_section) jumps
    # to the section whose prefix-stripped slug matches the key.
    view = _build_help_view(qapp)
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]

    view.open_section("troubleshooting")
    assert labels[view._current] == "Troubleshooting"
    view.open_section("ping")
    assert labels[view._current] == "Ping"


def test_search_highlights_all_matches_on_page(qapp) -> None:
    view = _build_help_view(qapp)
    view._search.setText("PingPair")
    view._on_find()
    # Every occurrence on the landed page is marked via extra selections.
    assert len(view._browser.extraSelections()) >= 1

    # Clearing the search removes the highlights.
    view._search.setText("")
    assert view._browser.extraSelections() == []


# ----- figure click-to-zoom --------------------------------------------


def _force_light(view) -> None:
    """Pin the palette to Light so screenshot resolution is deterministic."""
    from PySide6.QtGui import QColor, QPalette

    pal = view.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    view.setPalette(pal)


def test_resolve_shot_path_finds_theme_matched_screenshot(qapp) -> None:
    view = _build_help_view(qapp)
    _force_light(view)
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]
    view._sidebar.setCurrentRow(labels.index("Setup"))

    path = view._resolve_shot_path("01-checks-overview.png")
    assert path is not None and path.is_file()
    # Resolves under <theme>/<role>/<section-key>/ — Light + the default
    # (Undecided -> client) role + the "setup" key.
    assert path.parts[-4:] == ("light", "client", "setup", "01-checks-overview.png")

    # A name with no matching file is a safe miss (no exception).
    assert view._resolve_shot_path("nope-does-not-exist.png") is None


def test_resolve_shot_path_is_role_matched(qapp) -> None:
    """The Help guide shows the capture matching this PC's running role."""
    from pingpair.context import Role

    view = _build_help_view(qapp)
    _force_light(view)
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]
    view._sidebar.setCurrentRow(labels.index("Run"))

    for role, folder in (
        (Role.SERVER, "server"),
        (Role.CLIENT, "client"),
        (Role.LOOPBACK, "loopback"),
    ):
        view.ctx.run_state.role = role
        view._render(view._current)  # re-render picks up the new role
        path = view._resolve_shot_path("01-overview.png")
        assert path is not None and path.is_file(), f"{role} run shot missing"
        assert path.parts[-4:] == ("light", folder, "run", "01-overview.png")


def test_zoom_link_opens_full_screen_viewer(qapp) -> None:
    from PySide6.QtCore import QUrl

    # Round-23: the viewer moved to the shared _image_zoom module.
    from pingpair.views._image_zoom import ImageZoomDialog

    view = _build_help_view(qapp)
    _force_light(view)
    labels = [view._sidebar.item(i).text() for i in range(view._sidebar.count())]
    view._sidebar.setCurrentRow(labels.index("Setup"))

    assert view._zoom_dialog is None
    view._on_anchor_clicked(QUrl("zoom:01-checks-overview.png"))
    assert isinstance(view._zoom_dialog, ImageZoomDialog)

    # Fit <-> Actual-size toggle flips state cleanly.
    dlg = view._zoom_dialog
    assert dlg._fit is True
    dlg._toggle_zoom()
    assert dlg._fit is False
    dlg._toggle_zoom()
    assert dlg._fit is True
    dlg.close()


def test_zoom_link_with_missing_file_is_noop(qapp) -> None:
    from PySide6.QtCore import QUrl

    view = _build_help_view(qapp)
    _force_light(view)
    # A figure whose file isn't present must not open a viewer or crash.
    view._on_anchor_clicked(QUrl("zoom:not-a-real-shot.png"))
    assert view._zoom_dialog is None
