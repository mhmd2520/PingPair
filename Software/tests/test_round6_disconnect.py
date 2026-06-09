"""Round-6 regression tests — Server-killed-mid-case + Stop responsiveness.

Three scenarios that historically hung the Client / Server for 90+ s:

1. **Server dies mid-case** (Task W): the Client must notice via OSError
   on the next socket op, emit ``error`` + ``sweep_finished``, and return
   from ``run_sweep`` promptly instead of grinding through N more cases
   each timing out for ~90 s.

2. **Server stop with active client** (Task X): ``ControlServer.stop()``
   must shut down the live client socket so a handler blocked on
   ``read_message`` wakes up. The serve_forever thread must join within
   ~3 s (was ~120 s).

3. **Connect to unreachable Server** (Task W early-return): even if the
   Client never establishes the TCP control channel, it must emit both
   ``error`` *and* ``sweep_finished`` so the GUI's finished-handler runs.

Qt parts (the _qt_runner glue) are intentionally not exercised here —
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
from pingpair.core import case as case_module
from pingpair.core.control import client as client_module
from pingpair.core.control import server as server_module
from pingpair.core.control.protocol import FramedSocket, Message
from pingpair.core.control.client import ControlClient
from pingpair.core.control.server import ControlServer


# --- helpers (copied from test_control_loopback for self-containment) -----


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
    """Stand-in for ProcRunner that pretends iperf3 -s ran instantly."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def wait(self, timeout_s: float | None = None) -> _FakeRunResult:
        return _FakeRunResult()

    def stop(self, *_args, **_kwargs) -> None:
        pass


@contextmanager
def _server_on(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ControlServer, threading.Thread, list]]:
    """Spawn the Server in a thread bound to 127.0.0.1:port.

    Yields (srv, thread, events) so individual tests can call
    ``srv.stop()`` themselves and inspect the thread state.
    """
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


class _FakeCaseRunner:
    """Stand-in for CaseRunner.

    Returns a CaseResult-like sentinel without touching iperf3/fping.
    The Client's :meth:`_run_one_case` only uses
    ``.ok`` + ``.error``, so a minimal duck-typed object is fine.
    """

    def __init__(self, cfg, case, *, loopback, on_line=None) -> None:
        self.case = case

    def run(self):  # noqa: D401 — same shape as the real CaseRunner.run
        class _Res:
            def __init__(self, case) -> None:
                self.case = case
                self.error = None
                self.iperf_client = object()
                self.fping = object()

            @property
            def ok(self) -> bool:
                return True

        return _Res(self.case)

    def stop(self) -> None:
        pass


# --- Test 1 — Server killed mid-case ----------------------------------------


def test_server_killed_mid_case_emits_terminal_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server dies after SERVER_READY but before SERVER_RESULT.

    The Client should detect the dead socket on the read after CASE_DONE,
    record entry.error, then break out of the case loop and emit both
    ``error`` and ``sweep_finished`` events. The whole run_sweep call
    must return well under the old 90 s per-case timeout — even with
    the read_message timeout being case.duration_s*2 + 30, we lean on
    the socket shutdown waking that read immediately.
    """
    port = _free_port()
    monkeypatch.setattr(client_module, "CaseRunner", _FakeCaseRunner)

    # Patch the real Server's handler so it kills itself after sending
    # SERVER_READY for the first case, simulating a process death right
    # before the Client gets its SERVER_RESULT back.
    real_handle = server_module.ControlServer._handle_client

    def killing_handle(self, sock, addr):  # noqa: ANN001
        framed = FramedSocket(sock)
        self._client_sock = sock
        try:
            hello = framed.read_message(timeout_s=5.0)
            framed.write_message(
                Message.hello_ok(server_version="test")
            )
            # Drain START_SWEEP (one-way; may or may not appear).
            sweep_or_case = framed.read_message(timeout_s=5.0)
            if sweep_or_case.type == "START_SWEEP":
                case_msg = framed.read_message(timeout_s=5.0)
            else:
                case_msg = sweep_or_case
            framed.write_message(
                Message.server_ready(case_idx=case_msg.payload["case_idx"], pid=12345)
            )
            # Now nuke the connection — emulate Server process dying.
            threading.Thread(target=self.stop, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._client_sock = None

    monkeypatch.setattr(
        server_module.ControlServer, "_handle_client", killing_handle
    )

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
        t.start()
        t.join(timeout=15.0)
        assert not t.is_alive(), "Client run_sweep hung after Server death"

    # The socket close raced with the SERVER_READY recv loop; the client
    # must surface both events even on the unhappy path.
    names = [e[0] for e in client_events]
    assert "error" in names, f"expected error event, got: {names}"
    assert "sweep_finished" in names, f"expected sweep_finished, got: {names}"


# --- Test 2 — Server stop with active client --------------------------------


def test_stop_returns_quickly_with_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ControlServer.stop()`` while a handler is mid-recv joins fast.

    Before Task X, ``_handle_client`` could block on
    ``read_message(timeout_s=120.0)`` and ``stop()`` would only close
    the listen socket — the existing recv kept blocking for the full
    timeout. The fix snapshots and shuts down ``_client_sock`` so the
    inner recv wakes immediately.
    """
    port = _free_port()

    with _server_on(port, monkeypatch) as (srv, thread, events):
        # Raw socket — finish handshake so the server settles into the
        # blocked recv waiting for our next message.
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(Message.hello(client_version="test"))
        ack = framed.read_message(timeout_s=5.0)
        assert ack.type == "HELLO_OK"

        # Give the server a beat to record client_connected.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(n == "client_connected" for n, _ in events):
                break
            time.sleep(0.05)
        assert any(n == "client_connected" for n, _ in events), (
            "server never emitted client_connected"
        )

        # Now stop the server. Should join within a few seconds, not 120.
        t0 = time.monotonic()
        srv.stop()
        thread.join(timeout=3.0)
        elapsed = time.monotonic() - t0
        assert not thread.is_alive(), (
            f"server thread did not join after stop() (elapsed={elapsed:.2f}s)"
        )
        assert elapsed < 3.0, f"stop() took {elapsed:.2f}s (expected <3s)"

        try:
            sock.close()
        except OSError:
            pass


# --- Test 3 — Unreachable Server early-return -------------------------------


def test_run_sweep_with_unreachable_server_emits_sweep_finished() -> None:
    """No listener on the port → connect retries fail → terminal events.

    Round-6 (Task W) added a ``sweep_finished`` emit on the early-return
    path so the Qt finished-handler always fires, even when the Client
    never established the control channel.
    """
    port = _free_port()
    # Nothing is binding `port`. Connect attempts will refuse fast.

    cfg = load_default_config()
    cfg.network.control_port = port  # type: ignore[assignment]

    events: list[tuple[str, dict]] = []
    client = ControlClient(
        cfg, on_event=lambda n, d: events.append((n, d))
    )

    # _connect_with_retry uses 1.0s backoff x 3 attempts = ~3s worst case.
    # 20 s timeout gives generous headroom for slow CI machines.
    t0 = time.monotonic()
    sweep = client.run_sweep(
        server_host="127.0.0.1", selected_indexes=[1]
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 20.0, f"run_sweep took {elapsed:.2f}s on unreachable host"
    assert sweep.cases == [], "expected zero cases run"

    names = [e[0] for e in events]
    assert "error" in names, f"missing error event: {names}"
    assert "sweep_finished" in names, f"missing sweep_finished event: {names}"
    # And the ordering — error must precede sweep_finished so listeners
    # that latch the last error string have it set before they handle
    # the terminal event.
    err_idx = names.index("error")
    fin_idx = names.index("sweep_finished")
    assert err_idx < fin_idx, (
        f"error must come before sweep_finished, got {names}"
    )
