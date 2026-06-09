"""Round-22 fixes — CCC / DDD / EEE / FFF / GGG.

Reported by Mohamed after a VM session:

* **CCC / DDD — diagrams.** The Figma topology exports had rounded frames that
  left near-white corner triangles ("curvy borders / white corners"). All
  help diagrams are now rendered by ``tools/build_help_diagrams.py`` as
  square-bordered, theme-matched PNGs (no rounded corners, no white bleed),
  and new workflow diagrams (workflow / quickstart / case-grid /
  report-artifacts / control-sequence) are embedded in the guide.
* **EEE — welcome screen.** A first-boot Welcome screen (Quick Start / Skip)
  with a flash-card Quick-Start tour; shown once (``app/welcome_seen``), always
  replayable from Help.
* **FFF — Setup detail wording.** The prerequisite detail sentences were
  shortened so they fit one elided line (the Ethernet detail no longer trails
  off as "depends on this ph…").
* **GGG — Analysis tab.** Dropped the Case# spin-range and the per-value
  payload / bandwidth tick-boxes (now integer input fields, blank = all), and
  removed the "Uncheck all" + "Rename label" buttons.
"""

from __future__ import annotations

import logging
import sys
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from pingpair.config import load_default_config
from pingpair.context import Role

# ===========================================================================
# FFF — Setup detail wording (pure, no Qt).
# ===========================================================================

_FakeAddr = namedtuple("_FakeAddr", ["family", "address"])
_FakeStat = namedtuple("_FakeStat", ["isup"])


class _Family:
    def __init__(self, name: str, value: int = 2) -> None:
        self.name = name
        self.value = value


def test_ethernet_link_down_detail_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """The link-down detail dropped the long 'depends on this physical link'
    tail and now fits one line, while keeping the key word 'unplugged'."""
    from pingpair.core import prereq

    fake = SimpleNamespace(
        net_if_addrs=lambda: {"Ethernet": [_FakeAddr(_Family("AF_INET"), "169.254.1.1")]},
        net_if_stats=lambda: {"Ethernet": _FakeStat(isup=False)},
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_ethernet_cable(Role.CLIENT)
    assert "unplugged" in r.detail.lower()
    assert "depends on this physical link" not in r.detail
    assert len(r.detail) <= 95, f"detail too long to fit one line: {r.detail!r}"


def test_firewall_warn_detail_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys as _sys

    from pingpair.core import prereq

    # Force the WARN branch deterministically: Windows + the rule absent. Without
    # this the test depends on the host — on Linux it SKIPs, and on a Windows box
    # where PingPair's firewall rule already exists (after running the app) it
    # hits the PASS branch — both of which lack "click Fix to add one".
    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(prereq, "_netsh_rule_exists", lambda _name: (False, ""))
    r = prereq._check_firewall_rule(
        "Firewall: ICMP echo (ping)", "icmp", "open_icmp", Role.UNDECIDED
    )
    # The verbose tail was trimmed (the rule name itself is the only long bit).
    assert "may still allow traffic" not in r.detail
    assert "for clarity" not in r.detail
    assert "click Fix to add one" in r.detail


def test_wifi_detail_restore_note_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Wi-Fi reassurance note was trimmed to one short clause."""
    from pingpair.core import prereq

    monkeypatch.setattr(prereq.sys, "platform", "win32")
    monkeypatch.setattr(prereq, "_wifi_adapters_with_ipv4", lambda: [])
    r = prereq.check_wifi_off(Role.CLIENT)
    assert "Auto re-enabled" in r.detail
    assert len(r.detail) <= 110, f"wifi detail too long: {r.detail!r}"


# ===========================================================================
# CCC / DDD — diagrams exist, are square (no rounded/white corners).
# ===========================================================================

_ASSETS = (
    Path(__file__).resolve().parent.parent
    / "src" / "pingpair" / "resources" / "help" / "_assets"
)
_DIAGRAMS = (
    "topology", "topology-loopback", "workflow", "quickstart",
    "case-grid", "report-artifacts", "control-sequence",
)
_CANVAS_RGB = {"dark": (11, 18, 32), "light": (255, 255, 255)}  # #0b1220 / #ffffff


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_all_diagrams_present(theme: str) -> None:
    for name in _DIAGRAMS:
        p = _ASSETS / theme / f"{name}.png"
        assert p.is_file(), f"missing diagram {p}"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_diagram_corners_are_square_not_white(theme: str) -> None:
    """Every corner pixel must equal the theme's canvas colour — proving the
    frame is square (no rounded corner) with no near-white bleed (CCC)."""
    pytest.importorskip("PIL", reason="Pillow needed to inspect PNG corners")
    from PIL import Image

    want = _CANVAS_RGB[theme]
    for name in _DIAGRAMS:
        im = Image.open(_ASSETS / theme / f"{name}.png").convert("RGB")
        w, h = im.size
        for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            assert im.getpixel(corner) == want, (
                f"{theme}/{name}.png corner {corner} is {im.getpixel(corner)}, "
                f"expected square canvas {want}"
            )


# ===========================================================================
# Qt-backed tests (offscreen platform via conftest.py).
# ===========================================================================

pytest.importorskip("PySide6", reason="Round-22 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="Analysis view builds pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ----- EEE: welcome cards + dialog -----------------------------------------


def test_welcome_cards_shape() -> None:
    """Round-24: intro is minimal (no image); card 1 is the topology diagram;
    the rest reference real _shots screenshots, each resolved against the card's
    pinned role (or 'client' when unpinned). The card-5 finish popup is a known
    pending asset, so its absence is tolerated."""
    from pingpair import welcome_cards as wc

    assert wc.INTRO.image is None, "intro is minimal: logo + version + buttons"
    assert len(wc.QUICK_START_CARDS) == 7  # +Loopback Setup card (2026-06-02)
    assert wc.QUICK_START_CARDS[0].image == "topology.png", "card 1 = topology"
    help_root = _ASSETS.parent  # resources/help
    # The finish popup was delivered in the 2026-06-02 EXT batch, so every
    # card's screenshot now exists on disk for both themes — nothing pending.
    pending: set[str] = set()
    for card in wc.QUICK_START_CARDS:
        if not card.image:
            continue
        if "/" in card.image:
            # Real screenshot -> _shots/<theme>/<role>/<section>/<file>.
            role = card.role or "client"
            for theme in ("dark", "light"):
                p = help_root / "_shots" / theme / role / card.image
                assert p.is_file() or card.image in pending, f"missing real screenshot {p}"
        else:
            # Rendered diagram -> _assets/<theme>/<file>.
            for theme in ("dark", "light"):
                assert (_ASSETS / theme / card.image).is_file(), card.image


def test_welcome_dialog_navigation(qapp) -> None:
    from pingpair.views.welcome import WelcomeDialog

    dlg = WelcomeDialog(dark=True)
    assert dlg._index == -1, "opens on the intro/welcome card"
    assert not dlg._prev.isVisible() or dlg._index == -1

    total = len(__import__("pingpair.welcome_cards", fromlist=["x"]).QUICK_START_CARDS)
    dlg._go_next()  # intro -> first card
    assert dlg._index == 0
    for _ in range(total - 1):
        dlg._go_next()
    assert dlg._index == total - 1, "Next walks to the last card"
    assert "Got it" in dlg._next.text(), "last card's Next finishes the tour"

    dlg._go_prev()
    assert dlg._index == total - 2, "Previous steps back through the cards"


def test_welcome_dialog_starts_in_tour(qapp) -> None:
    from pingpair.views.welcome import WelcomeDialog

    dlg = WelcomeDialog(dark=False, start_in_tour=True)
    assert dlg._index == 0, "Help replay opens straight on the first flash card"


def test_welcome_seen_setting_roundtrip(qapp) -> None:
    from pingpair import settings

    # Default-false on a fresh key, persists True. (Uses the real QSettings
    # store; restore afterwards so we don't strand state for other tests.)
    original = settings.load_welcome_seen()
    try:
        settings.save_welcome_seen(True)
        assert settings.load_welcome_seen() is True
        settings.save_welcome_seen(False)
        assert settings.load_welcome_seen() is False
    finally:
        settings.save_welcome_seen(original)


# ----- GGG: Analysis filters + view ----------------------------------------


def test_analysis_filters_have_no_case_spinbox(qapp) -> None:
    from PySide6.QtWidgets import QSpinBox

    from pingpair.views._analysis_filters import AnalysisFilters

    filters = AnalysisFilters()
    assert not filters.findChildren(QSpinBox), (
        "the Case# spin-range was dropped (GGG) — no QSpinBox should remain"
    )
    # Payload + bandwidth are now plain integer-list input fields.
    assert hasattr(filters, "_payload_edit")
    assert hasattr(filters, "_bandwidth_edit")
    assert not hasattr(filters, "_payload_checks")
    assert not hasattr(filters, "_bandwidth_checks")


def test_analysis_filters_integer_only_and_blank_is_all(qapp) -> None:
    from pingpair.views._analysis_filters import AnalysisFilters

    filters = AnalysisFilters()
    case = SimpleNamespace(case_idx=1, payload_bytes=600, bandwidth_mbps_pushed=30)
    # Blank fields = keep every case.
    assert filters.case_passes(case)
    # A non-matching payload allow-list excludes the case.
    filters._payload_edit.setText("200, 1000")
    assert not filters.case_passes(case)
    filters._payload_edit.setText("600")
    assert filters.case_passes(case)
    # Bandwidth allow-list likewise.
    filters._bandwidth_edit.setText("10, 90")
    assert not filters.case_passes(case)


def test_analysis_filters_describe_active(qapp) -> None:
    from pingpair.views._analysis_filters import AnalysisFilters

    filters = AnalysisFilters()
    assert filters.describe_active() == "no filters (all cases)"
    filters._payload_edit.setText("200, 600")
    desc = filters.describe_active()
    assert "payloads" in desc and "200" in desc and "600" in desc


def test_analysis_run_list_has_no_buttons(qapp, tmp_path) -> None:
    """Round-23 (point 16): the 'Loaded runs' box has NO buttons at all —
    Select all / Remove (and the earlier Uncheck all / Rename label) are gone,
    so the whole box is the run-name list."""
    from PySide6.QtWidgets import QPushButton

    from pingpair.context import AppContext, RunState
    from pingpair.views.analysis_view import AnalysisView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round22"),
        run_state=RunState(role=Role.CLIENT),
    )
    ctx.run_state.report_dir = tmp_path
    view = AnalysisView(ctx)
    assert not hasattr(view, "_rename_btn")
    assert not hasattr(view, "_remove_btn")
    labels = {b.text() for b in view.findChildren(QPushButton)}
    for gone in ("Select all", "Remove", "Uncheck all", "Rename label"):
        assert gone not in labels, f"{gone!r} should be removed from the run list"
    # 2026-05-31: the Source section (folder field + Browse / Refresh / Add file…)
    # was removed — the list auto-refreshes from the Save Options destination.
    for gone in ("Browse…", "Refresh", "Add file…"):
        assert gone not in labels, f"{gone!r} should be gone with the Source section"
