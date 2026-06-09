"""Round-25 fixes — live Help search, welcome polish, Help split mirror, Analysis sizing.

Reported by Mohamed (img-1..5) after a VM session:

* **OOO** — Help search runs as you type; the Find button is gone. (point 3)
* **PPP** — the welcome intro shows a short brief again. (point 4)
* **QQQ** — the welcome step counter is high-contrast, not grey. (point 5)
* **RRR** — the welcome card type is larger. (point 6)
* **SSS** — Setup cards are ordered Server (2) before Client (3); card 4 reworded. (7/8)
* **TTT** — Help Quick Start mirrors the welcome cards, vertically. (9A)
* **UUU** — Help Overview shows all 7 diagrams in a deliberate order. (9B)
* **VVV** — Analysis Loaded-runs list is taller with a bigger font. (point 11)
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
# TTT / UUU — Help content split (pure, no Qt).
# ===========================================================================


def test_quick_start_mirrors_the_welcome_cards() -> None:
    """Quick Start carries the same step-by-step narrative as the welcome tour,
    Server before Client before Loopback (point 9A + 7; Loopback added
    2026-06-02)."""
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR

    by_key = {s.key: s for s in list_sections(HELP_DIR)}
    qs = by_key["quick-start"].index_path.read_text(encoding="utf-8")
    for heading in (
        "How it works", "Setup — go green (Server)", "Setup — go green (Client)",
        "Setup — go green (Loopback)",
        "Run the sweep", "Save the report", "Where to find more",
    ):
        assert heading in qs, f"Quick Start should walk '{heading}'"
    assert qs.index("(Server)") < qs.index("(Client)") < qs.index("(Loopback)"), (
        "Setup steps go Server -> Client -> Loopback"
    )


def test_overview_has_all_seven_diagrams_in_order() -> None:
    """Overview shows every diagram, ordered physical → logical → output (9B)."""
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR

    by_key = {s.key: s for s in list_sections(HELP_DIR)}
    ov = by_key["overview"].index_path.read_text(encoding="utf-8")
    order = [
        "topology.png", "topology-loopback.png", "workflow.png",
        "quickstart.png", "control-sequence.png", "case-grid.png",
        "report-artifacts.png",
    ]
    positions = []
    for name in order:
        assert name in ov, f"Overview missing {name}"
        positions.append(ov.index(f'src="{name}"'))
    assert positions == sorted(positions), "diagrams appear in the intended order"


# ===========================================================================
# Qt-backed tests (offscreen platform via conftest.py).
# ===========================================================================

pytest.importorskip("PySide6", reason="Round-25 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="views build pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _help_view(qapp):
    from pingpair.context import AppContext, RunState
    from pingpair.views.help_view import HelpView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round25"),
        run_state=RunState(role=Role.CLIENT),
    )
    return HelpView(ctx)


# ----- OOO: live Help search ------------------------------------------------


def test_help_has_no_find_button(qapp) -> None:
    from PySide6.QtWidgets import QPushButton

    view = _help_view(qapp)
    labels = {b.text() for b in view.findChildren(QPushButton)}
    assert "Find" not in labels, "the Find button was removed (point 3)"
    # Prev / Next match-steppers stay.
    assert {"Prev", "Next"} <= labels


def test_help_search_is_debounced_live(qapp) -> None:
    view = _help_view(qapp)
    assert hasattr(view, "_search_timer")
    assert view._search_timer.isSingleShot()
    # Typing arms the debounce timer (a search will fire shortly after).
    view._search.setText("topology")
    assert view._search_timer.isActive(), "typing schedules an auto-search"
    # Clearing the box stops the timer and drops the search.
    view._search.setText("")
    assert not view._search_timer.isActive()
    assert view._results.text() == ""


# ----- welcome dialog: PPP / QQQ / RRR / SSS -------------------------------


def _welcome(qapp, **kw):
    from pingpair.views.welcome import WelcomeDialog

    return WelcomeDialog(dark=True, role="client", **kw)


def test_welcome_intro_shows_brief_label(qapp) -> None:
    from PySide6.QtWidgets import QLabel

    dlg = _welcome(qapp)
    texts = " ".join(lbl.text() for lbl in dlg._intro_panel.findChildren(QLabel))
    assert "20-case" in texts, "the intro renders the welcoming brief (point 4)"


def test_welcome_step_counter_is_high_contrast(qapp) -> None:
    dlg = _welcome(qapp)
    assert "palette(mid)" not in dlg._step.styleSheet(), (
        "step counter is no longer the low-contrast grey (point 5)"
    )


def test_welcome_body_font_is_larger(qapp) -> None:
    dlg = _welcome(qapp)
    # Round-26 applied the size via `p` + a `<body>` wrapper so it actually
    # renders; Round-28 (point 3) dialled it 15pt -> 13pt for the card vibe.
    assert "font-size: 13pt" in dlg._css()


def test_welcome_setup_cards_server_before_client(qapp) -> None:
    dlg = _welcome(qapp)
    dlg._go_next()  # intro -> card 1 (How it works)
    dlg._go_next()  # -> card 2
    assert "Server" in dlg._heading.text(), "card 2 is the Server setup (point 7)"
    dlg._go_next()  # -> card 3
    assert "Client" in dlg._heading.text(), "card 3 is the Client setup (point 7)"


# ----- VVV: Analysis Loaded-runs sizing ------------------------------------


def test_analysis_runs_list_is_taller_and_larger(qapp, tmp_path) -> None:
    from pingpair.context import AppContext, RunState
    from pingpair.views.analysis_view import AnalysisView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round25-analysis"),
        run_state=RunState(role=Role.CLIENT),
    )
    ctx.run_state.report_dir = tmp_path
    view = AnalysisView(ctx)
    assert view._runs_list.font().pointSize() >= 11, "run names are larger (point 11)"
    # 2026-05-31: the list is now sized to its CONTENT (no big empty gap / no
    # box-inside-box), not a fixed-tall box. It shows at least ~3 rows.
    row_h = view._runs_list.fontMetrics().height()
    assert view._runs_list.height() >= 3 * row_h, "still tall enough for a few rows"
