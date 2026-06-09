"""Round-24 fixes — welcome rework, Help split, crisp figures, filter declutter.

Reported by Mohamed (img-1..5) after a VM session:

* **HHH** — the welcome intro is minimal & centred: logo + wordmark + version +
  Skip / Quick Start (no arrow), nothing else. (2D)
* **III** — Esc no longer dismisses the welcome tour. (2A)
* **JJJ** — larger, readable type across the cards. (2B)
* **KKK** — six cards; Setup split Client / Server (each pinned to that role's
  screenshot); card 5 is the finish popup; copy rewritten. (2C)
* **LLL** — inline figures are smooth-scaled so they read as sharply as the
  "Zoom More" view. (2E/2F)
* **MMM** — the Analysis "Reset filters" button is gone. (point 3)
* **NNN** — Help splits "Overview" into Quick Start + Overview; sidebar order is
  Quick Start → Overview → Setup → … (point 4)
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pingpair.config import load_default_config
from pingpair.context import Role

_HELP = (
    Path(__file__).resolve().parent.parent
    / "src" / "pingpair" / "resources" / "help"
)


# ===========================================================================
# NNN — Help split (pure, no Qt).
# ===========================================================================


def test_help_has_quick_start_then_overview_then_setup() -> None:
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR

    sections = list_sections(HELP_DIR)
    keys = [s.key for s in sections]
    assert "quick-start" in keys, "a dedicated Quick Start section exists (point 4)"
    assert "overview" in keys
    assert keys.index("quick-start") < keys.index("overview") < keys.index("setup"), (
        "sidebar order is Quick Start -> Overview -> Setup -> ..."
    )
    by_key = {s.key: s for s in sections}
    assert by_key["quick-start"].title == "Quick Start"
    assert by_key["overview"].title == "Overview"


def test_quick_start_is_the_tour_overview_is_the_diagrams() -> None:
    """Quick Start carries the getting-started flow; Overview is the diagram
    gallery (point 4A / 4B)."""
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR

    by_key = {s.key: s for s in list_sections(HELP_DIR)}
    qs = by_key["quick-start"].index_path.read_text(encoding="utf-8")
    # Round-26 (point 9): Quick Start is generated from the welcome cards; it
    # embeds the topology diagram (card 1) and carries the Setup steps.
    assert "topology.png" in qs
    assert "Fix all" in qs, "the setup steps are on Quick Start"

    ov = by_key["overview"].index_path.read_text(encoding="utf-8")
    for diagram in (
        "workflow.png", "case-grid.png", "control-sequence.png",
        "report-artifacts.png", "topology.png", "topology-loopback.png",
    ):
        assert diagram in ov, f"Overview should show {diagram} (point 4B)"


# ===========================================================================
# KKK — welcome card content (pure, no Qt).
# ===========================================================================


def test_welcome_intro_has_brief() -> None:
    # Round-26 (point 4): the intro carries a 2-3 sentence welcoming brief —
    # what the app is, who it's for, what sets it apart.
    from pingpair import welcome_cards as wc

    assert wc.INTRO.image is None
    body = wc.INTRO.body_html
    assert body.startswith("Welcome to PingPair")
    assert "20-case" in body
    assert "different" in body, "the brief says what makes it unique"
    assert 200 < len(body) < 900, "a 2-3 sentence brief, not a one-liner"


def test_welcome_has_seven_cards_with_split_setup() -> None:
    # Round-25 (points 7/8): Setup is split Server (card 2) BEFORE Client
    # (card 3) — configure the listener first. 2026-06-02: a Loopback Setup
    # card (4) joined them so all three roles are walked; Run/Save/More shifted
    # down to 5/6/7.
    from pingpair import welcome_cards as wc

    cards = wc.QUICK_START_CARDS
    assert len(cards) == 7, "How it works / Setup Server·Client·Loopback / Run / Save / More"
    assert cards[0].image == "topology.png"
    assert "(Server)" in cards[1].title and cards[1].role == "server"
    assert "(Client)" in cards[2].title and cards[2].role == "client"
    assert "(Loopback)" in cards[3].title and cards[3].role == "loopback"
    assert cards[1].image == "setup/01-checks-overview.png"
    assert cards[2].image == "setup/01-checks-overview.png"
    assert cards[3].image == "setup/01-checks-overview.png"
    # Run (card 5) + Save (card 6) are pinned to Client so they always show the
    # Client panels, never the running role (e.g. Loopback). (2026-06-02, task 3.)
    assert cards[4].image == "run/01-overview.png" and cards[4].role == "client"
    assert cards[5].image == "save-options/02-finish-popup.png" and cards[5].role == "client"
    # Card 5 (Run) no longer says "Back on the Client" (Client was a prior card).
    assert "Back on the" not in cards[4].body_html


# ===========================================================================
# Qt-backed tests (offscreen platform via conftest.py).
# ===========================================================================

pytest.importorskip("PySide6", reason="Round-24 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="views build pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _welcome(qapp, **kw):
    from pingpair.views.welcome import WelcomeDialog

    return WelcomeDialog(dark=True, role="client", **kw)


# ----- HHH: minimal centred intro ------------------------------------------


def test_welcome_intro_panel_minimal(qapp) -> None:
    dlg = _welcome(qapp)
    assert dlg._index == -1
    assert dlg._intro_panel.isVisibleTo(dlg), "intro panel shows on the welcome card"
    assert not dlg._tour_panel.isVisibleTo(dlg), "tour panel hidden on the welcome card"
    assert dlg._next.text() == "Quick Start", "no arrow on the intro's Quick Start (2D)"
    assert not dlg._prev.isVisible()


def test_welcome_enters_tour_swaps_panels(qapp) -> None:
    dlg = _welcome(qapp)
    dlg._go_next()  # intro -> card 0
    assert dlg._index == 0
    assert dlg._tour_panel.isVisibleTo(dlg)
    assert not dlg._intro_panel.isVisibleTo(dlg)


# ----- III: Esc does not dismiss -------------------------------------------


def test_escape_does_not_dismiss(qapp) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    dlg = _welcome(qapp)
    rejected: list[bool] = []
    dlg.rejected.connect(lambda: rejected.append(True))
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(ev)
    assert ev.isAccepted(), "Esc is swallowed, not routed to reject() (point 2A)"
    assert dlg._index == -1, "still on the intro — nothing dismissed"
    assert not rejected, "Esc did not trigger reject() (the dialog stays open)"


def test_arrow_keys_navigate(qapp) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    dlg = _welcome(qapp)
    right = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(right)
    assert dlg._index == 0, "Right advances from intro into the tour"
    left = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(left)
    assert dlg._index == -1, "Left steps back to the intro"


# ----- JJJ: larger fonts ----------------------------------------------------


def test_welcome_uses_larger_body_font(qapp) -> None:
    dlg = _welcome(qapp)
    css = dlg._css()
    # Round-26 (point 6): the size lives on `p`, not just `body` (a bare
    # fragment ignored the `body` selector). See test_round26 for the
    # rendered-size check that proves it actually applies.
    # Round-28 (point 3): dialled back from 15 pt to 13 pt to match the card vibe
    # (15 pt was set while fighting the 9 pt render bug; it read oversized once
    # it rendered honestly). The `p`-selector + `<body>`-wrap fix stays.
    assert "font-size: 13pt" in css, "body type sized for the card vibe"


# ----- KKK: role-pinned screenshot resolution ------------------------------


def test_resolve_image_honours_role_override(qapp) -> None:
    dlg = _welcome(qapp)  # running role = client
    client = dlg._resolve_image("setup/01-checks-overview.png", role="client")
    server = dlg._resolve_image("setup/01-checks-overview.png", role="server")
    assert client is not None and "/client/" in client.as_posix()
    assert server is not None and "/server/" in server.as_posix(), (
        "the Server card pins to the server shot regardless of running role (2C3)"
    )


def test_missing_card_image_renders_text_only(qapp) -> None:
    """The card-5 finish popup may not be present yet — the card must still
    render (text only), never a broken image."""
    from pingpair.welcome_cards import Card

    dlg = _welcome(qapp)
    card = Card(title="x", body_html="<p>hello</p>", image="save-options/does-not-exist.png")
    html = dlg._card_html(card)
    assert "hello" in html
    assert "<img" not in html, "no image tag when the file is absent"


# ----- LLL: crisp image embedding ------------------------------------------


def test_embed_scaled_image_downscales_only(qapp, tmp_path) -> None:
    from PySide6.QtGui import QPixmap, QTextDocument

    from pingpair.views._image_zoom import embed_scaled_image

    big = QPixmap(2000, 1000)
    big.fill()
    big_path = tmp_path / "big.png"
    big.save(str(big_path))
    doc = QTextDocument()
    res = embed_scaled_image(doc, str(big_path), max_logical_width=800,
                             device_pixel_ratio=1.0, key="mem://x")
    assert res == ("mem://x", 800), "wide source is smooth-scaled down to the target"

    small = QPixmap(400, 200)
    small.fill()
    small_path = tmp_path / "small.png"
    small.save(str(small_path))
    res2 = embed_scaled_image(doc, str(small_path), max_logical_width=800,
                              device_pixel_ratio=1.0, key="mem://y")
    assert res2 == ("mem://y", 400), "a small source is never upscaled (would blur)"

    assert embed_scaled_image(doc, str(tmp_path / "nope.png"), max_logical_width=800,
                              device_pixel_ratio=1.0, key="mem://z") is None


def test_help_inline_images_become_resources(qapp) -> None:
    from pingpair.context import AppContext, RunState
    from pingpair.views.help_view import HelpView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round24-help"),
        run_state=RunState(role=Role.CLIENT),
    )
    view = HelpView(ctx)
    out = view._embed_inline_images('<img src="topology.png" width="900">')
    assert 'src="mem://help/' in out, "resolvable inline figure becomes a scaled resource"
    untouched = view._embed_inline_images('<img src="not-a-real-figure.png">')
    assert untouched == '<img src="not-a-real-figure.png">', "unknown figure left as-is"


# ----- MMM: Analysis Reset filters removed ---------------------------------


def test_analysis_filters_have_no_reset_button(qapp) -> None:
    from PySide6.QtWidgets import QPushButton

    from pingpair.views._analysis_filters import AnalysisFilters

    filters = AnalysisFilters()
    labels = {b.text() for b in filters.findChildren(QPushButton)}
    assert "Reset filters" not in labels, "the Reset filters button was removed (point 3)"
    assert not filters.findChildren(QPushButton), "the filter box has no buttons now"
    # The programmatic reset API is kept.
    assert hasattr(filters, "reset_all")
