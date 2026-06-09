"""Round-23 fixes — welcome rework, shared zoom viewer, Help/Analysis tweaks.

Reported by Mohamed (Image-1..8):

* Welcome: minimal intro (logo + version + 2 buttons), main window hidden until
  the tour ends, maximised, topology on card 1, real screenshots on cards 2-4,
  reworded card 5, ← arrow on Previous. (points 2-5, 9, 17)
* Shared maximised zoom viewer reused by Help AND Welcome; its toggle renamed
  "Actual size (100%)" → "Zoom More"; every figure (diagram or screenshot) is
  clickable to zoom. (points 11-15)
* Help: Replay button removed; the diagrams section is titled "Overview". (9, 10)
* Analysis: the 'Loaded runs' box has no buttons. (16) — see test_round22.
"""

from __future__ import annotations

import logging

import pytest

from pingpair.config import load_default_config
from pingpair.context import Role

pytest.importorskip("PySide6", reason="Round-23 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="views build pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ----- shared zoom viewer (points 11-13) -----------------------------------


def test_zoom_toggle_is_named_zoom_more(qapp) -> None:
    from PySide6.QtGui import QPixmap

    from pingpair.views._image_zoom import ImageZoomDialog

    pm = QPixmap(640, 360)
    pm.fill()
    dlg = ImageZoomDialog(pm, "demo", dark=True)
    assert dlg._toggle.text() == "Zoom More", (
        "fit-to-window default offers 'Zoom More' (renamed from 'Actual size (100%)')"
    )
    dlg._toggle_zoom()
    assert dlg._toggle.text() == "Fit to window"


def test_make_images_zoomable_wraps_bare_only(qapp) -> None:
    from pingpair.views.help_view import _make_images_zoomable

    html = (
        '<div class="figure"><a href="zoom:shot.png">'
        '<img src="shot.png" width="900"></a></div>'
        '<div class="figure"><img src="topology.png" width="880"></div>'
    )
    out = _make_images_zoomable(html)
    # The bare diagram img is now wrapped...
    assert '<a href="zoom:topology.png"><img src="topology.png"' in out
    # ...and the already-wrapped screenshot is NOT double-wrapped.
    assert out.count('href="zoom:shot.png"') == 1


# ----- Help tab (points 9, 10) ---------------------------------------------


def test_help_overview_section_titled_overview() -> None:
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR

    sections = {s.key: s for s in list_sections(HELP_DIR)}
    assert "overview" in sections
    assert sections["overview"].title == "Overview", (
        "the diagrams section is titled 'Overview' (point 10)"
    )


def test_help_has_no_replay_button(qapp) -> None:
    from PySide6.QtWidgets import QPushButton

    from pingpair.context import AppContext, RunState
    from pingpair.views.help_view import HelpView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round23-help"),
        run_state=RunState(role=Role.CLIENT),
    )
    view = HelpView(ctx)
    labels = {b.text() for b in view.findChildren(QPushButton)}
    assert not any("Replay" in label for label in labels), (
        "the 'Replay Quick Start' button was removed (point 9)"
    )


# ----- Welcome dialog (points 2-5, 15, 17) ---------------------------------


def _welcome(qapp, **kw):
    from pingpair.views.welcome import WelcomeDialog

    return WelcomeDialog(dark=True, role="client", **kw)


def test_welcome_intro_is_minimal(qapp) -> None:
    from pingpair import welcome_cards as wc

    assert wc.INTRO.image is None
    dlg = _welcome(qapp)
    assert dlg._index == -1
    assert dlg._next.text().startswith("Quick Start")
    assert not dlg._prev.isVisible(), "intro hides Previous"


def test_welcome_is_maximized(qapp) -> None:
    from PySide6.QtCore import Qt

    dlg = _welcome(qapp)
    assert dlg.windowState() & Qt.WindowState.WindowMaximized, (
        "welcome opens maximised (point 4)"
    )


def test_welcome_previous_has_back_arrow(qapp) -> None:
    dlg = _welcome(qapp)
    assert "Previous" in dlg._prev.text()
    assert dlg._prev.text().lstrip().startswith("←"), (
        "Previous carries a back-arrow (point 17)"
    )


def test_welcome_resolves_diagram_and_screenshot(qapp) -> None:
    dlg = _welcome(qapp)
    topo = dlg._resolve_image("topology.png")
    assert topo is not None and topo.is_file(), "card-1 topology resolves from _assets"
    shot = dlg._resolve_image("setup/01-checks-overview.png")
    assert shot is not None and shot.is_file(), "real Setup screenshot resolves from _shots"


def test_welcome_image_click_opens_zoom(qapp) -> None:
    from PySide6.QtCore import QUrl

    dlg = _welcome(qapp)
    topo = dlg._resolve_image("topology.png")
    assert topo is not None
    dlg._on_anchor_clicked(QUrl(f"zoom:{topo.as_posix()}"))
    assert dlg._zoom_dialog is not None, "clicking a figure opens the zoom viewer (point 15)"


def test_welcome_navigation_walks_all_cards(qapp) -> None:
    from pingpair.welcome_cards import QUICK_START_CARDS

    dlg = _welcome(qapp)
    total = len(QUICK_START_CARDS)
    dlg._go_next()  # intro -> card 0
    assert dlg._index == 0
    for _ in range(total - 1):
        dlg._go_next()
    assert dlg._index == total - 1
    assert "Got it" in dlg._next.text()
