"""Round-7 Task DD — fast Server-kill detection via background socket monitor.

Before DD, the Client called ``runner.run()`` (a 30 s blocking iperf3) and
only noticed a dead Server *after* the case finished, when the next
socket op tried to send CASE_DONE. Symptom: when the Server was killed at
case-start (t=0), the Client still ground out the full ~30 s before
emitting ``error`` + ``sweep_finished``.

Task DD added a background monitor thread on the Client that polls the
control socket via a zero-byte recv. When the recv returns EOF / OSError
the monitor sets a flag *and* calls ``runner.stop()`` on the active
CaseRunner so iperf3 winds down immediately.

This regression test proves the end-to-end flow:

1. Real ControlServer on 127.0.0.1.
2. Patched CaseRunner that simulates a 30 s iperf3 run, polling a
   stop event in 0.1 s ticks so the monitor can wake it early.
3. After the Client has been mid-``runner.run()`` for ~2 s, call
   ``srv.stop()`` to simulate the Server process dying.
4. Assert ``run_sweep`` returns within ~5 s (was ~30 s pre-DD).
5. Assert the fake CaseRunner saw ``.stop()`` called.
6. Assert the resulting SweepResult holds 1 case with an error string
   that mentions the disconnect, and both ``error`` and
   ``sweep_finished`` events fired.

Qt parts (the ``_qt_runner`` glue) are intentionally not exercised here —
the sandbox has no PySide6.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from pingpair.config import load_default_config
from pingpair.core.case import CaseResult
from pingpair.core.control import client as client_module
from pingpair.core.control import server as server_module
from pingpair.core.control.client import ControlClient
from pingpair.core.control.server import ControlServer


# --- helpers (replicated from test_round6_disconnect for self-containment) ---


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeRunResult:
    """Duck-typed RunResult — Client only reads .returncode / .stdout."""

    def __init__(self, rc: int = 0) -> None:
        self.returncode = rc
        self.stdout = '{"fake": true}'
        self.stderr = ""


class _FakeProcRunner:
    """Stand-in for ProcRunner on the *Server* side (iperf3 -s pretend)."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def wait(self, timeout_s: float | None = None) -> _FakeRunResult:
        return _FakeRunResult()

    def stop(self, *_args, **_kwargs) -> None:
        pass


# Module-level list so the test can introspect the fake CaseRunner
# instances that the Client spawned during run_sweep.
_FAKE_RUNNERS: list["_FakeCaseRunner"] = []


class _FakeCaseRunner:
    """Stand-in for the *Client*-side CaseRunner.

    Simulates a long-running iperf3 (30 s) by polling a stop Event in
    0.1 s ticks. ``.stop()`` wakes the loop immediately — which is
    exactly what the Round-7 monitor thread is supposed to do when it
    notices the control socket has gone away.
    """

    def __init__(self, cfg, case, *, loopback, on_line=None) -> None:
        self.cfg = cfg
        self.case = case
        self.loopback = loopback
        self.on_line = on_line
        self._stop = threading.Event()
        self.was_stopped = False
        self.run_started_at: float | None = None
        self.run_ended_at: float | None = None
        _FAKE_RUNNERS.append(self)

    def run(self) -> CaseResult:
        self.run_started_at = time.monotonic()
        deadline = self.run_started_at + 30.0
        while time.monotonic() < deadline:
            if self._stop.wait(0.1):
                self.was_stopped = True
                break
        self.run_ended_at = time.monotonic()

        # Build a minimal CaseResult. iperf_client = None and fping =
        # None makes .ok return False, which is fine — the test cares
        # about timing + .stop() being called, not result content.
        rr = _FakeRunResult()
        return CaseResult(
            case=self.case,
            iperf_client=None,
            iperf_intervals=[],
            iperf_server_raw="",
            fping=None,
            iperf_client_run=rr,  # type: ignore[arg-type]
            fping_run=rr,  # type: ignore[arg-type]
            iperf_server_run=None,
            error=None,
        )

    def stop(self) -> None:
        self._stop.set()


@contextmanager
def _server_on(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ControlServer, threading.Thread, list]]:
    """Spawn a real ControlServer on 127.0.0.1:port for the test."""
    monkeypatch.setattr(server_module, "ProcRunner", _FakeProcRunner)

    cfg = load_default_config()
    cfg.network.control_port = port  # type: ignore[assignment]

    events: list[tuple[str, dict]] = []
    srv = ControlServer(cfg, on_event=lambda n, d: events.append((n, d)))

    thread = threading.Thread(
        target=srv.serve_forever,
        kwargs={"bind_host": "127.0.0.1"},
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        srv.stop()
        thread.join(timeout=2.0)
        pytest.fail(f"Server failed to bind 127.0.0.1:{port}")

    try:
        yield srv, thread, events
    finally:
        srv.stop()
        thread.join(timeout=3.0)


# --- The regression test ----------------------------------------------------


def test_fast_disconnect_aborts_iperf_via_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server killed mid-iperf — Client must abort in < 5 s, not ~30 s."""
    _FAKE_RUNNERS.clear()
    port = _free_port()
    monkeypatch.setattr(client_module, "CaseRunner", _FakeCaseRunner)

    with _server_on(port, monkeypatch) as (srv, _thread, _events):
        cfg = load_default_config()
        cfg.network.control_port = port  # type: ignore[assignment]

        client_events: list[tuple[str, dict]] = []
        client = ControlClient(
            cfg, on_event=lambda n, d: client_events.append((n, d))
        )

        result: dict = {}

        def _runner() -> None:
            result["sweep"] = client.run_sweep(
                server_host="127.0.0.1", selected_indexes=[1]
            )

        t = threading.Thread(target=_runner, daemon=True)
        sweep_started = time.monotonic()
        t.start()

        # Wait for the fake CaseRunner to be constructed AND
        # ``run()`` to start. ~2 s should be plenty for HELLO +
        # START_SWEEP + START_CASE + SERVER_READY + runner instantiation.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (
                _FAKE_RUNNERS
                and _FAKE_RUNNERS[-1].run_started_at is not None
            ):
                break
            time.sleep(0.05)
        else:
            srv.stop()
            t.join(timeout=5.0)
            pytest.fail(
                "Client never entered runner.run() — wiring broken"
            )

        # Give the runner ~2 s of fake iperf3 time so we're clearly
        # mid-case when the Server dies.
        time.sleep(2.0)

        # Kill the Server. The Client's monitor thread should detect
        # the closed socket within ~0.5 s and call .stop() on the
        # active CaseRunner.
        kill_at = time.monotonic()
        srv.stop()

        # Hard deadline so the test can never hang.
        t.join(timeout=10.0)
        sweep_elapsed = time.monotonic() - sweep_started
        time_since_kill = time.monotonic() - kill_at

        assert not t.is_alive(), (
            f"Client run_sweep hung after Server kill "
            f"(elapsed={sweep_elapsed:.1f} s)"
        )

    # --- assertions ---------------------------------------------------

    # 1. Fast abort — Round-7 target is well under 5 s post-kill;
    #    pre-fix this was ~28 s (remainder of the 30 s fake iperf).
    assert time_since_kill < 5.0, (
        f"run_sweep took {time_since_kill:.1f} s to return after kill — "
        f"expected < 5 s. Monitor thread did not abort runner early."
    )

    # 2. The fake runner saw .stop() called.
    assert _FAKE_RUNNERS, "no FakeCaseRunner instances were created"
    runner = _FAKE_RUNNERS[-1]
    assert runner.was_stopped, (
        "FakeCaseRunner.stop() was never called — monitor missed the "
        "disconnect or didn't wire through to the runner."
    )

    # 3. The runner woke up *well* before the 30 s deadline.
    assert runner.run_started_at is not None
    assert runner.run_ended_at is not None
    runner_duration = runner.run_ended_at - runner.run_started_at
    assert runner_duration < 10.0, (
        f"runner.run() blocked for {runner_duration:.1f} s — "
        f"expected early exit after .stop()."
    )

    # 4. Terminal events both fired, and the sweep contains 1 case
    #    with an error string that mentions the disconnect.
    names = [n for n, _ in client_events]
    assert "error" in names, f"expected error event, got: {names}"
    assert "sweep_finished" in names, (
        f"expected sweep_finished, got: {names}"
    )

    sweep = result.get("sweep")
    assert sweep is not None, "run_sweep returned None"
    assert len(sweep.cases) == 1, (
        f"expected 1 case in sweep, got {len(sweep.cases)}"
    )
    entry = sweep.cases[0]
    assert entry.error, "case entry should carry an error after disconnect"
    err_lower = entry.error.lower()
    assert any(
        token in err_lower
        for token in ("disconnect", "connection", "socket", "eof", "reset", "closed", "stopped", "broken")
    ), f"error string doesn't mention disconnect: {entry.error!r}"
