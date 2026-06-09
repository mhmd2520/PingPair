"""Guarding tests for the 2026-06-04 HIGH-batch review fixes.

- **H2** — the Wi-Fi crash-recovery marker survives a *failed* close-time
  re-enable (so the next launch retries), and is cleared on success.
- **H3** — a NEW warning re-asserts the cross-tab banner even after a prior
  Dismiss (a dismiss must not suppress every future warning all session).
- **H5** — ``fmt()`` / ``fmt_delta()`` render non-finite (nan/inf) as the
  em-dash, never the literal text ``"nan"`` in a report cell.
- **H6** — the comma-separated int-list validator rejects ``0`` so the
  field's red-border state agrees with what Apply accepts.

(H4 — moving the manual report save / comparison export off the GUI thread —
is exercised through the existing auto-save path and has no direct unit test.)
"""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

from pingpair.analysis.chart_renderer import ascii_sparkline
from pingpair.analysis.stats import fmt, fmt_delta
from pingpair.views._validators import _diagnose_int_list


# ===========================================================================
# H5 — fmt non-finite guard (pure)
# ===========================================================================


def test_fmt_renders_non_finite_as_dash() -> None:
    assert fmt(float("nan")) == "—"
    assert fmt(float("inf")) == "—"
    assert fmt(float("-inf")) == "—"
    assert fmt(None) == "—"
    # Finite values are unaffected.
    assert fmt(1.2345) == "1.23"
    assert fmt(1.2345, decimals=3) == "1.234"


def test_fmt_delta_renders_non_finite_as_dash() -> None:
    assert fmt_delta(float("nan")) == "—"
    assert fmt_delta(float("inf")) == "—"
    assert fmt_delta(None) == "—"
    # Finite values keep their signed format.
    assert fmt_delta(-1.5) == "−1.50"
    assert fmt_delta(1.5) == "+1.50"


def test_ascii_sparkline_handles_non_finite_without_raising() -> None:
    # nan FIRST is the crash trigger: before the fix it poisoned min()/span
    # and int(round(nan)) raised ValueError, breaking the txt appendix.
    out = ascii_sparkline([float("nan"), 1.0, 2.0, float("inf"), 3.0])
    assert len(out) == 5  # one glyph per sample, always
    assert out[0] == " "  # nan -> gap
    assert out[3] == " "  # inf -> gap
    assert out[2] != " " and out[4] != " "  # finite values still render a block


def test_ascii_sparkline_all_non_finite_is_blank() -> None:
    assert ascii_sparkline([float("nan"), None, float("inf")]) == "   "


# ===========================================================================
# H6 — int-list validator rejects 0 (agrees with config_view._parse_int_list)
# ===========================================================================


def test_diagnose_int_list_rejects_bare_zero() -> None:
    ok, msg = _diagnose_int_list("0")
    assert ok is False
    assert "positive" in msg.lower()


def test_diagnose_int_list_rejects_zero_within_list() -> None:
    ok, _ = _diagnose_int_list("200, 0, 1000")
    assert ok is False


def test_diagnose_int_list_accepts_valid_lists() -> None:
    assert _diagnose_int_list("200, 600, 1000")[0] is True
    assert _diagnose_int_list("1")[0] is True


def test_diagnose_int_list_still_flags_leading_zero() -> None:
    ok, msg = _diagnose_int_list("01")
    assert ok is False
    assert "leading zero" in msg.lower()


# ===========================================================================
# H2 / H3 — MainWindow methods (driven via the unbound method on a fake self,
# so we exercise the real logic without building the heavy full window).
# ===========================================================================

pytest.importorskip("PySide6", reason="MainWindow method tests need Qt")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ----- H3: a new warning unsuppresses the banner after a dismiss -----------


def _fake_banner_window():
    from PySide6.QtWidgets import QLabel, QWidget

    from pingpair.context import RunState

    rs = RunState()
    fake = SimpleNamespace(
        ctx=SimpleNamespace(run_state=rs),
        _warning_dismissed=False,
        _last_warning_text="",
        _warning_label=QLabel(),
        _warning_row=QWidget(),
    )
    return fake, rs


def test_new_warning_reasserts_banner_after_dismiss(qapp) -> None:
    from pingpair.app import MainWindow

    fake, rs = _fake_banner_window()

    # A role-IP warning shows the banner.
    rs.role_warning_text = "expected 192.168.1.2 to be bound, but it isn't"
    MainWindow.refresh_warning_banner(fake)
    assert fake._warning_row.isVisible()

    # User dismisses it.
    MainWindow._on_dismiss_warning(fake)
    assert fake._warning_dismissed is True
    assert not fake._warning_row.isVisible()

    # The SAME warning re-firing stays dismissed.
    MainWindow.refresh_warning_banner(fake)
    assert not fake._warning_row.isVisible()

    # A genuinely NEW warning (mid-sweep connection error) re-asserts the
    # banner despite the earlier dismiss — the bug this fix closes.
    rs.connection_warning_text = "Server connection error: link down"
    MainWindow.refresh_warning_banner(fake)
    assert fake._warning_row.isVisible()
    assert "link down" in fake._warning_label.text()


# ----- H2: Wi-Fi marker survives a failed close-time re-enable -------------


def _fake_close_window(adapter: str = "Wi-Fi"):
    from pingpair.context import RunState

    rs = RunState()
    rs.wifi_offline_adapter = adapter
    return SimpleNamespace(
        ctx=SimpleNamespace(run_state=rs, logger=logging.getLogger("test-wifi"))
    )


def _patch_wifi_deps(monkeypatch):
    import pingpair.core.fix_actions as fa
    import pingpair.core.state_capture as sc
    import pingpair.core.winexec as we
    import pingpair.settings as settings_mod

    monkeypatch.setattr(fa, "is_admin", lambda: True)
    monkeypatch.setattr(sc, "_build_enable_wifi_argv", lambda a: ["netsh", "stub"])
    monkeypatch.setattr(we, "harden_argv", lambda argv: argv)
    saved: list = []
    monkeypatch.setattr(
        settings_mod, "save_wifi_offline_adapter", lambda v: saved.append(v)
    )
    return saved


def test_wifi_marker_cleared_on_successful_reenable(qapp, monkeypatch) -> None:
    from pingpair.app import MainWindow

    saved = _patch_wifi_deps(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: SimpleNamespace())

    fake = _fake_close_window()
    MainWindow._maybe_restore_wifi_on_close(fake)

    assert saved == [None], "marker must be cleared on a successful re-enable"
    assert fake.ctx.run_state.wifi_offline_adapter is None


def test_wifi_marker_survives_failed_reenable(qapp, monkeypatch) -> None:
    from pingpair.app import MainWindow

    saved = _patch_wifi_deps(monkeypatch)

    def _boom(*a, **k):
        raise OSError("netsh failed to launch")

    monkeypatch.setattr(subprocess, "Popen", _boom)

    fake = _fake_close_window()
    MainWindow._maybe_restore_wifi_on_close(fake)  # must not raise

    assert saved == [], "marker must NOT be cleared when the re-enable failed"
    assert fake.ctx.run_state.wifi_offline_adapter == "Wi-Fi", (
        "the marker must survive so the next launch retries the restore"
    )
