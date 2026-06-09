"""Setup-tab live carrier watcher (2026-06-04).

While the Setup tab is visible, a 2 s timer samples just the NICs' link
state (``isup``) and auto re-runs the full prereq pass the moment it flips —
so a cable unplug/replug updates the table without a manual Re-check. Only
the cheap snapshot runs each tick; ``force_refresh`` fires only on a change.

These exercise the two pieces of logic directly: the snapshot
(``_sample_link_signature``, a staticmethod) and the per-tick decision
(``_on_link_watch_tick``, driven via the unbound method on a fake self so we
don't build the heavy full SetupView, whose __init__ spawns a real
prereq-check QThread). The showEvent/hideEvent start/stop wiring is trivial
and verified on the VM.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

# Importing the view module needs Qt present (it imports PySide6 widgets).
pytest.importorskip("PySide6", reason="setup_view imports PySide6")

from pingpair.views.setup_view import SetupView  # noqa: E402


# ===========================================================================
# _sample_link_signature — the cheap per-tick snapshot
# ===========================================================================


def test_sample_link_signature_reflects_isup(monkeypatch) -> None:
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(
        psutil,
        "net_if_stats",
        lambda: {
            "Wi-Fi": SimpleNamespace(isup=False),
            "Ethernet": SimpleNamespace(isup=True),
        },
    )
    # Sorted by interface name, carrier coerced to bool.
    assert SetupView._sample_link_signature() == (
        ("Ethernet", True),
        ("Wi-Fi", False),
    )


def test_sample_link_signature_flips_on_unplug(monkeypatch) -> None:
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(
        psutil, "net_if_stats", lambda: {"Ethernet": SimpleNamespace(isup=True)}
    )
    up = SetupView._sample_link_signature()
    monkeypatch.setattr(
        psutil, "net_if_stats", lambda: {"Ethernet": SimpleNamespace(isup=False)}
    )
    down = SetupView._sample_link_signature()
    assert up != down


def test_sample_link_signature_none_on_psutil_error(monkeypatch) -> None:
    psutil = pytest.importorskip("psutil")

    def _boom():
        raise OSError("psutil hiccup on an odd NIC")

    monkeypatch.setattr(psutil, "net_if_stats", _boom)
    assert SetupView._sample_link_signature() is None


# ===========================================================================
# _on_link_watch_tick — refresh only on a real change
# ===========================================================================


def _fake_setup(prev, sample, *, busy: bool = False):
    calls: list[str] = []
    fake = SimpleNamespace(
        _last_link_sig=prev,
        _sample_link_signature=lambda: sample,
        ctx=SimpleNamespace(logger=logging.getLogger("test-link-watch")),
        force_refresh=lambda: calls.append("refresh"),
        # Re-check disabled == a prereq pass or Fix-all is in flight.
        _recheck_btn=SimpleNamespace(isEnabled=lambda: not busy),
    )
    return fake, calls


def test_tick_refreshes_when_carrier_flips() -> None:
    fake, calls = _fake_setup(
        prev=(("Ethernet", True),), sample=(("Ethernet", False),)
    )
    SetupView._on_link_watch_tick(fake)
    assert calls == ["refresh"]
    assert fake._last_link_sig == (("Ethernet", False),)


def test_tick_no_refresh_when_unchanged() -> None:
    sig = (("Ethernet", True),)
    fake, calls = _fake_setup(prev=sig, sample=sig)
    SetupView._on_link_watch_tick(fake)
    assert calls == []


def test_tick_first_sample_seeds_baseline_without_refresh() -> None:
    # prev=None means "we just became visible" — seed, don't trigger.
    fake, calls = _fake_setup(prev=None, sample=(("Ethernet", True),))
    SetupView._on_link_watch_tick(fake)
    assert calls == []
    assert fake._last_link_sig == (("Ethernet", True),)


def test_tick_ignores_unavailable_snapshot() -> None:
    fake, calls = _fake_setup(prev=(("Ethernet", True),), sample=None)
    SetupView._on_link_watch_tick(fake)
    assert calls == []
    # Baseline preserved so the next good sample still diffs correctly.
    assert fake._last_link_sig == (("Ethernet", True),)


def test_tick_skips_refresh_while_busy_but_absorbs_signature() -> None:
    # A carrier flip while a prereq pass / Fix-all is running (Re-check
    # disabled) must NOT trigger a refresh — that would spawn a concurrent
    # worker and re-enable the buttons mid-Fix-all. The new signature is still
    # absorbed into the baseline so it won't double-fire once the op finishes.
    fake, calls = _fake_setup(
        prev=(("Ethernet", True),), sample=(("Ethernet", False),), busy=True
    )
    SetupView._on_link_watch_tick(fake)
    assert calls == []
    assert fake._last_link_sig == (("Ethernet", False),)
