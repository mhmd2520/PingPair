"""Round-26 — real font sizing, zoom-off, runs-box inset, Quick-Start↔cards sync.

Reported by Mohamed (img-1..5), quality-first:

* **point 4** — the intro brief is a 2-3 sentence description (also asserted in
  test_round24).
* **points 5/6** — the card body font now ACTUALLY renders large. Root cause: a
  bare ``<p>`` fragment ignored the ``body`` font-size selector and fell back to
  Qt's 9 pt default. Fixed by wrapping in ``<body>`` and putting the size on
  ``p``. Also: Ctrl+wheel no longer zooms the Welcome screen or the Help guide.
* **point 8** — the Loaded-runs list is inset from its group-box border.
* **point 9** — Help → Quick Start is generated from the welcome cards, so it
  can't drift from the tour.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from pingpair.config import load_default_config
from pingpair.context import Role

pytest.importorskip("PySide6", reason="Round-26 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="views build pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _welcome(qapp, **kw):
    from pingpair.views.welcome import WelcomeDialog

    return WelcomeDialog(dark=True, role="client", **kw)


def _first_text_pt(browser) -> float:
    """Rendered point size of the first non-empty text fragment."""
    block = browser.document().firstBlock()
    while block.isValid() and not block.text().strip():
        block = block.next()
    return block.begin().fragment().charFormat().font().pointSizeF()


# ----- points 5/6: the font actually renders large -------------------------


def test_card_body_actually_renders_at_set_size(qapp) -> None:
    """The real fix: measure the RENDERED size, not the CSS string. A bare
    fragment used to fall back to 9 pt; it must now render at the set size
    (13 pt since Round-28's point-3 reduction), proving the `<body>`-wrap +
    `p`-selector fix still binds."""
    dlg = _welcome(qapp)
    dlg._go_next()  # intro -> card 1
    pt = _first_text_pt(dlg._browser)
    assert 12.0 <= pt <= 14.0, f"card body should render ~13pt (not 9pt), got {pt}"


def test_card_html_is_body_wrapped(qapp) -> None:
    from pingpair.welcome_cards import QUICK_START_CARDS

    dlg = _welcome(qapp)
    html = dlg._card_html(QUICK_START_CARDS[0])
    assert html.startswith("<body>") and html.endswith("</body>"), (
        "card HTML is wrapped so the body font-size selector binds (point 6)"
    )


# ----- point 5: Ctrl+wheel does not zoom -----------------------------------


def test_welcome_and_help_use_no_zoom_browser(qapp) -> None:
    from pingpair.context import AppContext, RunState
    from pingpair.views._image_zoom import NoZoomTextBrowser
    from pingpair.views.help_view import HelpView

    dlg = _welcome(qapp)
    assert isinstance(dlg._browser, NoZoomTextBrowser)

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round26-help"),
        run_state=RunState(role=Role.CLIENT),
    )
    view = HelpView(ctx)
    assert isinstance(view._browser, NoZoomTextBrowser)


def test_ctrl_wheel_does_not_zoom(qapp) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    from pingpair.views._image_zoom import NoZoomTextBrowser

    b = NoZoomTextBrowser()
    b.document().setDefaultStyleSheet("body{font-size:15pt;} p{font-size:15pt;}")
    b.setHtml("<body><p>hello world</p></body>")
    before = _first_text_pt(b)
    ev = QWheelEvent(
        QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 240),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    b.wheelEvent(ev)
    assert ev.isAccepted(), "Ctrl+wheel is consumed (scrolls, never zooms)"
    assert _first_text_pt(b) == before, "font size unchanged after Ctrl+wheel"


# ----- point 8: Loaded-runs list is inset from the group box ---------------


def test_runs_list_inset_from_group_box(qapp, tmp_path) -> None:
    from PySide6.QtCore import Qt

    from pingpair.context import AppContext, RunState
    from pingpair.views.analysis_view import AnalysisView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round26-analysis"),
        run_state=RunState(role=Role.CLIENT),
    )
    ctx.run_state.report_dir = tmp_path
    view = AnalysisView(ctx)
    margins = view._runs_list.parentWidget().layout().contentsMargins()
    assert margins.left() >= 10 and margins.right() >= 10, (
        "the runs list is inset from the group-box border (point 8)"
    )
    # 2026-05-31: no horizontal scrollbar (it wouldn't reliably paint on the
    # user's Windows). Each row shows the run name on one line (elided if long),
    # full details in the tooltip — no scrollbar, no wrap.
    assert (
        view._runs_list.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    ), "no horizontal scrollbar"
    assert view._runs_list.wordWrap() is False


# ----- point 9: Quick Start is generated from the cards (no drift) ----------


def test_quick_start_html_matches_generator() -> None:
    """The committed Quick Start HTML must equal the generator's output, so it
    can never drift from the welcome cards (run tools/build_quick_start_help.py
    after editing the cards)."""
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools_dir))
    import build_quick_start_help as gen

    generated = gen.render_quick_start_html()
    committed = gen.OUT_PATH.read_text(encoding="utf-8")
    assert committed == generated, (
        "Quick Start HTML is stale — run tools/build_quick_start_help.py"
    )


def test_quick_start_carries_each_card_text() -> None:
    """Sanity: a distinctive sentence from every card appears in Quick Start."""
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR
    from pingpair.welcome_cards import QUICK_START_CARDS

    by_key = {s.key: s for s in list_sections(HELP_DIR)}
    qs = by_key["quick-start"].index_path.read_text(encoding="utf-8")
    for card in QUICK_START_CARDS:
        assert card.title in qs, f"Quick Start missing card heading {card.title!r}"
