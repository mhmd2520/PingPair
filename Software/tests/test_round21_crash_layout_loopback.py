"""Round-21 VM-test fixes — YY / ZZ / AAA / BBB.

Reported by Mohamed after a VM session (DHCP boot → Fix-all → repeated role
switches), captured in IMG\\.

**YY — the app *suddenly* vanished with no Python traceback.** The only
console evidence was Qt's ``QThread: Destroyed while thread '' is still
running`` — a C++ ``qFatal``/``abort``, distinct from the Round-20 Python
crash-guard. Root cause: ``SetupView._CheckWorker`` (and the ``PingView``
worker) emit their *custom* finished signal as the **last line of run()**,
i.e. while the QThread is still alive, and the slot wired to it nulled
``self._worker``. A close in that window made ``shutdown()`` find no worker
to ``wait()`` on, so app-teardown destroyed the still-running thread. The
reference is now dropped on the **built-in** ``finished`` signal (fires only
after the thread truly exits), mirroring ``_ServerPanel``.

**ZZ — Analysis tab metadata filters overlapped.** The Technician / Customer
/ Record-ID rows used a QFormLayout, which starves vertically when the left
pane is short until the labels overlap their inputs. Replaced with explicit
label+field rows that never collapse.

**AAA — Setup detail sentences wrapped to 2-3 lines; Fix buttons over-padded.**
Detail column word-wrap is off (one elided line, full text on hover); Fix
buttons carry the ``compact`` property so they hug their label.

**BBB — Loopback showed irrelevant Wi-Fi / firewall WARNs.** 127.0.0.1 never
touches the Windows Firewall or any physical NIC, so those checks now SKIP in
Loopback mode (matching the cable / IP / gateway checks).
"""

from __future__ import annotations

import logging

import pytest

from pingpair.config import load_default_config
from pingpair.context import Role

# ===========================================================================
# BBB — Loopback SKIPs the firewall + Wi-Fi checks (pure, no Qt).
# ===========================================================================


def test_loopback_skips_wifi_and_firewall_checks() -> None:
    from pingpair.core.prereq import (
        Status,
        check_firewall_control,
        check_firewall_icmp,
        check_firewall_iperf3,
        check_wifi_off,
    )

    for fn in (
        check_wifi_off,
        check_firewall_icmp,
        check_firewall_iperf3,
        check_firewall_control,
    ):
        result = fn(Role.LOOPBACK)
        assert result.status is Status.SKIP, f"{fn.__name__} must SKIP in Loopback"
        assert result.fix_action_id is None, (
            f"{fn.__name__} must offer no Fix in Loopback (nothing to fix)"
        )


def test_run_checks_loopback_marks_wifi_firewall_skip() -> None:
    """End-to-end: the run_checks wiring threads role through so a Loopback
    pass yields a clean SKIP/PASS table — nothing the user must 'fix'."""
    from pingpair.core.prereq import Status, run_checks

    results = run_checks(load_default_config(), Role.LOOPBACK)
    by_name = {r.name: r for r in results}
    for name in (
        "Wi-Fi disabled",
        "Firewall: ICMP echo (ping)",
        "Firewall: iperf3 (TCP/UDP 5201)",
        "Firewall: control channel (TCP 5202)",
    ):
        assert by_name[name].status is Status.SKIP, f"{name} must SKIP in Loopback"
    # Loopback must never produce a FAIL (it would block nothing, but the
    # red row is misleading): the worst it shows is SKIP/PASS.
    assert all(r.status is not Status.FAIL for r in results)


def test_undecided_firewall_checks_still_accept_no_role() -> None:
    """The default (CLI --check-prereqs) path keeps working: calling with no
    role must not raise and must not auto-SKIP via the loopback branch."""
    from pingpair.core.prereq import Status, check_firewall_iperf3

    r = check_firewall_iperf3()  # Role.UNDECIDED default
    # On the Linux test box this SKIPs for platform reasons, on Windows it
    # PASSes/WARNs — but never via the loopback branch, so the detail differs.
    assert r.status in {Status.SKIP, Status.PASS, Status.WARN}
    assert "Loopback" not in r.detail


# ===========================================================================
# Qt-backed tests (offscreen platform via conftest.py).
# ===========================================================================

pytest.importorskip("PySide6", reason="Round-21 GUI tests need Qt")
pytest.importorskip("pyqtgraph", reason="views build pyqtgraph plots")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _build_setup_view(qapp, monkeypatch):
    """Construct a real SetupView with run_checks stubbed fast, draining the
    auto-fired initial worker so later assertions start from a clean state."""
    from pingpair.context import AppContext, RunState
    from pingpair.views import setup_view as sv

    # Stub the check suite so __init__'s auto _refresh() returns instantly and
    # never shells out to netsh. Neutralise the external-IP modal so draining
    # the initial worker can't pop a blocking dialog under offscreen Qt.
    monkeypatch.setattr(sv, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(sv.SetupView, "_check_external_ip_change", lambda self: None)

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round21"),
        run_state=RunState(role=Role.CLIENT),
    )
    view = sv.SetupView(ctx)
    # Drain the initial _CheckWorker so it's truly finished before we poke
    # at self._worker (the very lifetime YY hardens).
    worker = view._worker
    if worker is not None:
        worker.wait(3000)
    for _ in range(20):
        qapp.processEvents()
    return view


# ----- YY: SetupView worker reference lifetime -----------------------------


def test_setup_on_checks_finished_does_not_drop_worker(qapp, monkeypatch) -> None:
    """The custom-signal handler must NOT null self._worker — that is the YY
    regression. (run() emits finished_with_results while still alive.)"""
    view = _build_setup_view(qapp, monkeypatch)
    sentinel = object()
    view._worker = sentinel  # type: ignore[assignment]
    view._on_checks_finished([])
    assert view._worker is sentinel, (
        "_on_checks_finished must keep the worker reference until the thread "
        "truly finishes (built-in finished), or a close mid-window aborts"
    )


def test_setup_clear_finished_worker_sender_guard(qapp, monkeypatch) -> None:
    view = _build_setup_view(qapp, monkeypatch)
    live = object()
    stale = object()
    view._worker = live  # type: ignore[assignment]

    view._clear_finished_worker(stale)
    assert view._worker is live, "a stale finished must not clear the live worker"

    view._clear_finished_worker(live)
    assert view._worker is None, "the live worker's finished must clear it"


def test_setup_clear_finished_worker_runs_pending_refresh(qapp, monkeypatch) -> None:
    view = _build_setup_view(qapp, monkeypatch)
    live = object()
    view._worker = live  # type: ignore[assignment]
    view._refresh_pending = True
    calls: list[int] = []
    view._refresh = lambda: calls.append(1)  # type: ignore[method-assign]

    view._clear_finished_worker(live)
    assert calls, "a pending re-run must fire once the old thread has exited"
    assert view._refresh_pending is False


# ----- AAA: Setup one-line details + compact Fix buttons -------------------


def test_setup_table_word_wrap_off(qapp, monkeypatch) -> None:
    view = _build_setup_view(qapp, monkeypatch)
    assert view._table.wordWrap() is False, (
        "Detail column must render on one elided line (full text in tooltip)"
    )


def test_setup_fix_button_is_compact(qapp, monkeypatch) -> None:
    from PySide6.QtWidgets import QPushButton

    from pingpair.core.prereq import CheckResult, Status

    view = _build_setup_view(qapp, monkeypatch)
    view._render_results([
        CheckResult(
            name="Firewall: ICMP echo (ping)",
            status=Status.WARN,
            detail="No explicit rule.",
            fix_action_id="open_icmp",
        )
    ])
    holder = view._table.cellWidget(0, 3)
    assert holder is not None, "a WARN row with a fix must render an Action button"
    button = holder.findChild(QPushButton)
    assert button is not None
    assert button.property("compact") is True, (
        "the Fix button must carry the compact property so it hugs its label"
    )


# ----- ZZ: Analysis filters no longer use a starving QFormLayout -----------


def test_analysis_filters_use_no_formlayout(qapp) -> None:
    from PySide6.QtWidgets import QFormLayout

    from pingpair.views._analysis_filters import AnalysisFilters

    filters = AnalysisFilters()
    assert not filters.findChildren(QFormLayout), (
        "the metadata rows must not use a QFormLayout (it starved + overlapped)"
    )
    # All three metadata fields are still present.
    assert set(filters._metadata_edits) == {"technician", "customer", "record_id"}


def test_analysis_metadata_filter_still_matches(qapp) -> None:
    """Behaviour preserved by the ZZ rewrite: a non-empty metadata filter
    still substring-matches (case-insensitive); empty = match any."""
    from types import SimpleNamespace

    from pingpair.views._analysis_filters import AnalysisFilters

    filters = AnalysisFilters()
    run = SimpleNamespace(metadata={"technician": "Alice", "customer": "Acme"})

    assert filters.run_passes_metadata(run), "empty filters pass everything"
    filters._metadata_edits["technician"].setText("ali")
    assert filters.run_passes_metadata(run), "case-insensitive substring match"
    filters._metadata_edits["technician"].setText("bob")
    assert not filters.run_passes_metadata(run), "non-match must be excluded"


# ----- YY: PingView worker reference lifetime ------------------------------


def test_ping_on_finished_does_not_drop_worker(qapp) -> None:
    """_on_finished (custom finished_ok handler) must not null self._worker —
    the reference now lives until the built-in finished (_on_thread_finished)."""
    from pingpair.context import AppContext, RunState
    from pingpair.views.ping_view import PingView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round21-ping"),
        run_state=RunState(role=Role.LOOPBACK),
    )
    view = PingView(ctx)
    sentinel = object()
    view._worker = sentinel  # type: ignore[assignment]
    view._on_finished(0, "")  # benign: no summary lines to parse
    assert view._worker is sentinel, (
        "_on_finished must not clear the worker; _on_thread_finished does"
    )
