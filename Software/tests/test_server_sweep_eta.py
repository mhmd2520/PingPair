"""Server-side whole-sweep ETA readout.

Mirrors the Client panel's "Sweep  <elapsed> elapsed · ~<left> left" line
(Feature 1) on the Server's Run tab, shown under "Cases received". The
Server never drives the schedule, but it receives the same
``sweep_starting`` / ``case_starting`` / ``case_done`` events the Client
does, so it times each case locally and renders the identical readout.

Two layers are covered:

* the pure :func:`pingpair.core.runner.sweep_time_left_s` helper now shared
  by both panels, and
* the ``_ServerPanel`` event wiring (built under the offscreen Qt platform,
  with ``_start_server`` patched out so no real listener thread spins up and
  ``time.monotonic`` pinned for a deterministic countdown).
"""

from __future__ import annotations

import logging

import pytest

from pingpair.core.runner import sweep_time_left_s

# ===========================================================================
# Pure helper
# ===========================================================================


def test_sweep_time_left_counts_in_flight_case() -> None:
    # First of 4 cases, 10 s budget each, 0 s elapsed → all four still to run.
    assert sweep_time_left_s(
        position=1, total=4, case_total_s=10.0, case_elapsed_s=0.0
    ) == 40.0


def test_sweep_time_left_subtracts_elapsed() -> None:
    # 3 s into the first of 4 → 4*10 - 3 = 37 s.
    assert sweep_time_left_s(
        position=1, total=4, case_total_s=10.0, case_elapsed_s=3.0
    ) == 37.0


def test_sweep_time_left_shrinks_as_sweep_advances() -> None:
    early = sweep_time_left_s(position=1, total=4, case_total_s=10.0, case_elapsed_s=0.0)
    late = sweep_time_left_s(position=3, total=4, case_total_s=10.0, case_elapsed_s=0.0)
    assert late < early


def test_sweep_time_left_last_case_is_one_budget() -> None:
    assert sweep_time_left_s(
        position=4, total=4, case_total_s=10.0, case_elapsed_s=0.0
    ) == 10.0


def test_sweep_time_left_never_negative() -> None:
    # A case running far past its estimate must not show a negative countdown.
    assert sweep_time_left_s(
        position=4, total=4, case_total_s=10.0, case_elapsed_s=999.0
    ) == 0.0


def test_sweep_time_left_single_case_sweep() -> None:
    # total=1 is a real config (one selected case): the in-flight case is the
    # whole sweep, so 0 s elapsed → exactly one budget, no off-by-one.
    assert sweep_time_left_s(
        position=1, total=1, case_total_s=10.0, case_elapsed_s=0.0
    ) == 10.0


@pytest.mark.parametrize("elapsed", [0.0, 4.0, 10.0])
def test_sweep_time_left_position_past_total_clamps_to_zero(elapsed: float) -> None:
    # Overshoot guard: a slow/extra case can push position beyond total
    # (e.g. position 5 of 4). ``remaining_cases = max(0, total-position+1)``
    # floors to 0 there — the in-flight case is counted *within*
    # remaining_cases (the +1), so once position>total there is no budget
    # and no separate remainder term: the readout is 0 for any elapsed,
    # never a negative or runaway value.
    assert sweep_time_left_s(
        position=5, total=4, case_total_s=10.0, case_elapsed_s=elapsed
    ) == 0.0


# ===========================================================================
# _ServerPanel event wiring (real widget, no listener thread)
# ===========================================================================

pytest.importorskip("PySide6", reason="server-ETA wiring needs Qt")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _build_server_panel(qapp, monkeypatch):
    """A real ``_ServerPanel`` with the auto-start listener patched out."""
    from pingpair.config import load_default_config
    from pingpair.context import AppContext, Role, RunState
    from pingpair.views import script_view

    # No real ControlServer thread / socket bind in a unit test.
    monkeypatch.setattr(script_view._ServerPanel, "_start_server", lambda self: None)

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-server-eta"),
        run_state=RunState(role=Role.SERVER),
    )
    return script_view._ServerPanel(ctx)


def test_idle_readout_before_any_sweep(qapp, monkeypatch) -> None:
    panel = _build_server_panel(qapp, monkeypatch)
    assert panel._remaining_label.text() == "(idle)"


def test_readout_counts_down_during_sweep(qapp, monkeypatch) -> None:
    from pingpair.views import script_view

    clock = {"t": 1000.0}
    monkeypatch.setattr(script_view.time, "monotonic", lambda: clock["t"])

    panel = _build_server_panel(qapp, monkeypatch)

    panel._on_event("sweep_starting", {"total_cases": 4})
    assert "waiting" in panel._remaining_label.text().lower()

    # Case 1 starts at t=1000; pin the per-case budget to 10 s so the
    # arithmetic is deterministic regardless of the default config.
    panel._on_event(
        "case_starting",
        {"case": "#01", "case_idx": 1, "position": 1, "total_cases": 4},
    )
    panel._completed_case_walls = [10.0]

    clock["t"] = 1003.0  # 3 s into case 1
    panel._on_eta_tick()
    # left = 4*10 - 3 = 37 s ; elapsed = 3 s
    assert panel._remaining_label.text() == "3s elapsed  ·  ~37s left"

    clock["t"] = 1006.0  # 6 s into case 1
    panel._on_eta_tick()
    assert panel._remaining_label.text() == "6s elapsed  ·  ~34s left"


def test_readout_elapsed_only_when_total_unknown(qapp, monkeypatch) -> None:
    """A legacy / minimal Client that sends no total_cases leaves
    ``_active_total`` None. The readout must then show elapsed-ONLY rather
    than freezing at the idle / 'waiting…' text while cases visibly run.
    """
    from pingpair.views import script_view

    clock = {"t": 2000.0}
    monkeypatch.setattr(script_view.time, "monotonic", lambda: clock["t"])

    panel = _build_server_panel(qapp, monkeypatch)
    # No sweep_starting (so _sweep_total stays None) and case_starting carries
    # no total_cases — exactly the legacy-client path.
    panel._on_event("case_starting", {"case": "#01", "case_idx": 1})
    assert panel._active_total is None  # precondition for this path

    clock["t"] = 2008.0  # 8 s into the sweep
    panel._on_eta_tick()
    text = panel._remaining_label.text()
    assert text == "8s elapsed", text
    assert "left" not in text  # can't estimate remaining without a total


def test_case_done_records_wall_and_freezes(qapp, monkeypatch) -> None:
    from pingpair.views import script_view

    clock = {"t": 500.0}
    monkeypatch.setattr(script_view.time, "monotonic", lambda: clock["t"])

    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("sweep_starting", {"total_cases": 4})
    panel._on_event(
        "case_starting",
        {"case": "#01", "case_idx": 1, "position": 1, "total_cases": 4},
    )

    clock["t"] = 512.0  # case 1 finished after 12 s
    frozen = panel._remaining_label.text()
    panel._on_event("case_done", {"case": "#01", "case_idx": 1, "returncode": 0})

    assert panel._completed_case_walls == [12.0], (
        "the measured per-case wall must be recorded for the adaptive estimate"
    )
    assert not panel._eta_timer.isActive(), (
        "the timer must stop in the inter-case gap so the readout freezes"
    )
    # The label is left frozen (not reset) until the next case starts.
    assert panel._remaining_label.text() == frozen


def test_sweep_finished_resets_to_idle(qapp, monkeypatch) -> None:
    from pingpair.views import script_view

    clock = {"t": 0.0}
    monkeypatch.setattr(script_view.time, "monotonic", lambda: clock["t"])

    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("sweep_starting", {"total_cases": 2})
    panel._on_event(
        "case_starting",
        {"case": "#01", "case_idx": 1, "position": 1, "total_cases": 2},
    )
    clock["t"] = 5.0
    panel._on_eta_tick()
    assert "left" in panel._remaining_label.text()

    panel._on_event("sweep_finished", {"cases": 2, "total_cases": 2})
    assert panel._remaining_label.text() == "(idle)"
    assert not panel._eta_timer.isActive()


def test_client_disconnect_resets_to_idle(qapp, monkeypatch) -> None:
    from pingpair.views import script_view

    clock = {"t": 0.0}
    monkeypatch.setattr(script_view.time, "monotonic", lambda: clock["t"])

    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("sweep_starting", {"total_cases": 2})
    panel._on_event(
        "case_starting",
        {"case": "#01", "case_idx": 1, "position": 1, "total_cases": 2},
    )
    panel._on_event("client_disconnected", {})

    assert panel._remaining_label.text() == "(idle)"
    assert not panel._eta_timer.isActive()
