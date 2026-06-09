"""Round-8 Task FF — user-initiated Stop on the Client must not surface
as a "Server disconnected" error.

Before Task FF, pressing Stop on the Client closed the control socket
(intentionally — so blocked recvs/sends unblock fast, Round-6 Task X),
which tripped the Round-7 background monitor's ``select`` with OSError.
The monitor flipped ``_socket_alive = False`` and the post-runner code
set ``entry.error = "Server disconnected during case (monitor)"`` →
``run_sweep`` emitted an ``error`` event → the orange "Server connection
error" banner appeared. WRONG: the user pressed Stop, not the server.

Task FF guards every socket-closed classifier path with a ``self._stop``
check. On user-stop the monitor exits cleanly, the entry is tagged
``"stopped by user"``, no error event fires, and the orange banner stays
clear.

This regression test proves the end-to-end flow:

1. Real ControlServer on 127.0.0.1.
2. Patched CaseRunner that simulates a 30 s iperf3 run, polling a
   stop event in 0.1 s ticks.
3. After the Client has been mid-``runner.run()`` for ~2 s, call
   ``client.stop()`` (the user-stop path) instead of ``srv.stop()``.
4. Assert ``run_sweep`` returns within ~5 s (Round-7 fast-abort path
   still works for user-stop).
5. Assert NO ``error`` event was emitted (the key Task FF guarantee).
6. Assert ``sweep_finished`` WAS emitted.
7. Assert the single case has ``entry.error`` containing ``"stopped by
   user"`` (not ``"Server disconnected"``).
8. Assert the fake CaseRunner saw ``.stop()`` called.

Qt parts are intentionally not exercised — the sandbox has no PySide6.
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


# --- helpers (replicated from test_round7_fast_disconnect for self-containment) ---


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeRunResult:
    def __init__(self, rc: int = 0) -> None:
        self.returncode = rc
        self.stdout = '{"fake": true}'
        self.stderr = ""


class _FakeProcRunner:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def wait(self, timeout_s: float | None = None) -> _FakeRunResult:
        return _FakeRunResult()

    def stop(self, *_args, **_kwargs) -> None:
        pass


_FAKE_RUNNERS: list["_FakeCaseRunner"] = []


class _FakeCaseRunner:
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


def test_user_stop_emits_no_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User Stop on the Client must NOT surface as 'Server disconnected'."""
    _FAKE_RUNNERS.clear()
    port = _free_port()
    monkeypatch.setattr(client_module, "CaseRunner", _FakeCaseRunner)

    with _server_on(port, monkeypatch) as (_srv, _thread, _events):
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

        # Wait for the fake CaseRunner to enter run().
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (
                _FAKE_RUNNERS
                and _FAKE_RUNNERS[-1].run_started_at is not None
            ):
                break
            time.sleep(0.05)
        else:
            client.stop()
            t.join(timeout=5.0)
            pytest.fail(
                "Client never entered runner.run() — wiring broken"
            )

        # ~2 s of fake iperf3 time so we're clearly mid-case.
        time.sleep(2.0)

        # User-initiated Stop on the Client (NOT the Server).
        stop_at = time.monotonic()
        client.stop()

        t.join(timeout=10.0)
        sweep_elapsed = time.monotonic() - sweep_started
        time_since_stop = time.monotonic() - stop_at

        assert not t.is_alive(), (
            f"Client run_sweep hung after user stop "
            f"(elapsed={sweep_elapsed:.1f} s)"
        )

    # --- assertions ---------------------------------------------------

    # 1. Fast termination.
    assert time_since_stop < 5.0, (
        f"run_sweep took {time_since_stop:.1f} s to return after "
        f"client.stop() — expected < 5 s."
    )

    # 2. The fake runner saw .stop() called (Round-6 path still works).
    assert _FAKE_RUNNERS, "no FakeCaseRunner instances were created"
    runner = _FAKE_RUNNERS[-1]
    assert runner.was_stopped, (
        "FakeCaseRunner.stop() was never called — Stop didn't propagate."
    )

    # 3. The KEY Task FF assertion: NO error event was emitted.
    names = [n for n, _ in client_events]
    assert "error" not in names, (
        f"User-stop must NOT emit an error event, but got: {names}. "
        f"Payloads: {[d for n, d in client_events if n == 'error']!r}"
    )

    # 4. sweep_finished still fires.
    assert "sweep_finished" in names, (
        f"expected sweep_finished, got: {names}"
    )

    # 5. The sweep contains 1 case tagged 'stopped by user' (not
    #    'Server disconnected').
    sweep = result.get("sweep")
    assert sweep is not None, "run_sweep returned None"
    assert len(sweep.cases) == 1, (
        f"expected 1 case in sweep, got {len(sweep.cases)}"
    )
    entry = sweep.cases[0]
    assert entry.error, "case entry should carry an error after user stop"
    err_lower = entry.error.lower()
    assert "stopped by user" in err_lower, (
        f"expected 'stopped by user' in entry.error, got: {entry.error!r}"
    )
    assert "server disconnect" not in err_lower, (
        f"entry.error should NOT mention server disconnect for a "
        f"user-initiated stop, got: {entry.error!r}"
    )
