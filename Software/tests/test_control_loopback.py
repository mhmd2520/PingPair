"""Test the Server↔Client handshake on 127.0.0.1.

We can't run real iperf3 cases in CI, so this test exercises the message
flow only: spawn a Server thread, monkey-patch the iperf3 spawn so the
server thinks the case completes instantly, then drive a Client through
one case and assert both sides exchanged the expected messages.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from pingpair.config import load_default_config
from pingpair.core.control import server as server_module
from pingpair.core.control.protocol import FramedSocket, Message
from pingpair.core.control.server import ControlServer


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
def _server_on(port: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[list]:
    """Spawn the Server in a thread bound to 127.0.0.1:port. Yields the
    captured event log."""
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

    # Wait until the listener is reachable.
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
        yield events
    finally:
        srv.stop()
        thread.join(timeout=2.0)


def test_handshake_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """HELLO → HELLO_OK then close cleanly."""
    port = _free_port()
    with _server_on(port, monkeypatch):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(Message.hello(client_version="test"))
        ack = framed.read_message(timeout_s=5.0)
        framed.close()
        assert ack.type == "HELLO_OK"
        assert "server_version" in ack.payload


def test_one_case_full_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive one START_CASE through the server with a fake iperf3 server."""
    port = _free_port()
    with _server_on(port, monkeypatch) as events:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)

        framed.write_message(Message.hello(client_version="test"))
        assert framed.read_message(timeout_s=5.0).type == "HELLO_OK"

        framed.write_message(
            Message.start_case(
                case_idx=1,
                payload_bytes=200,
                bandwidth_mbps=10,
                duration_s=1,
                protocol="udp",
                server_ip="127.0.0.1",
                client_ip="127.0.0.1",
            )
        )
        ready = framed.read_message(timeout_s=5.0)
        assert ready.type == "SERVER_READY"
        assert ready.payload["case_idx"] == 1

        framed.write_message(Message.case_done(case_idx=1))
        result = framed.read_message(timeout_s=5.0)
        assert result.type == "SERVER_RESULT"
        assert result.payload["case_idx"] == 1
        assert result.payload["returncode"] == 0

        framed.write_message(Message.finish())
        framed.close()

    # Confirm the server emitted the expected event sequence.
    names = [e[0] for e in events]
    assert "listening" in names
    assert "client_connected" in names
    assert "case_starting" in names
    assert "case_done" in names


def test_start_sweep_emits_event_with_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """START_SWEEP triggers a sweep_starting event carrying total_cases."""
    port = _free_port()
    with _server_on(port, monkeypatch) as events:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)

        framed.write_message(Message.hello(client_version="test"))
        assert framed.read_message(timeout_s=5.0).type == "HELLO_OK"

        framed.write_message(Message.start_sweep(total_cases=12, sweep_id="abc"))
        # No reply — START_SWEEP is one-way. Send FINISH so the server
        # closes cleanly and the event log gets fully drained.
        framed.write_message(Message.finish())
        framed.close()

    sweep_events = [d for n, d in events if n == "sweep_starting"]
    assert len(sweep_events) == 1
    assert sweep_events[0]["total_cases"] == 12
    assert sweep_events[0]["sweep_id"] == "abc"


def test_consecutive_sweeps_reset_server_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sweeps from the same listener: cases_received resets between them.

    This is the scenario from the screenshot — Mohamed ran case #01,
    disconnected, reconnected, ran case #13, and the title showed
    "(2/20)" because the old code carried the counter over. With
    START_SWEEP the second sweep's case_done should report
    cases_received=1 (not 2).
    """
    port = _free_port()
    with _server_on(port, monkeypatch) as events:
        # ---- sweep #1 ----
        sock1 = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        f1 = FramedSocket(sock1)
        f1.write_message(Message.hello(client_version="test"))
        f1.read_message(timeout_s=5.0)
        f1.write_message(Message.start_sweep(total_cases=1, sweep_id="s1"))
        f1.write_message(Message.start_case(
            case_idx=1, payload_bytes=200, bandwidth_mbps=10, duration_s=1,
            protocol="udp", server_ip="127.0.0.1", client_ip="127.0.0.1",
        ))
        assert f1.read_message(timeout_s=5.0).type == "SERVER_READY"
        f1.write_message(Message.case_done(case_idx=1))
        assert f1.read_message(timeout_s=5.0).type == "SERVER_RESULT"
        f1.write_message(Message.finish())
        f1.close()

        # ---- sweep #2 (new connection, same server) ----
        sock2 = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        f2 = FramedSocket(sock2)
        f2.write_message(Message.hello(client_version="test"))
        f2.read_message(timeout_s=5.0)
        f2.write_message(Message.start_sweep(total_cases=1, sweep_id="s2"))
        f2.write_message(Message.start_case(
            case_idx=13, payload_bytes=1000, bandwidth_mbps=50, duration_s=1,
            protocol="udp", server_ip="127.0.0.1", client_ip="127.0.0.1",
        ))
        assert f2.read_message(timeout_s=5.0).type == "SERVER_READY"
        f2.write_message(Message.case_done(case_idx=13))
        assert f2.read_message(timeout_s=5.0).type == "SERVER_RESULT"
        f2.write_message(Message.finish())
        f2.close()

    # case_done events should show cases_received==1 for *both* sweeps,
    # confirming the counter reset. Without START_SWEEP handling the
    # second sweep would report cases_received==2.
    case_done_events = [d for n, d in events if n == "case_done"]
    assert len(case_done_events) == 2
    assert case_done_events[0]["cases_received"] == 1
    assert case_done_events[1]["cases_received"] == 1
    # case_starting carries the in-sweep position too — both should be 1.
    case_starting_events = [d for n, d in events if n == "case_starting"]
    assert [e["position"] for e in case_starting_events] == [1, 1]
    # And the total_cases the Server learned matches what each Client sent.
    assert [e["total_cases"] for e in case_starting_events] == [1, 1]


def test_bad_handshake_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending START_CASE before HELLO yields an ERROR frame."""
    port = _free_port()
    with _server_on(port, monkeypatch):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(
            Message.start_case(
                case_idx=1, payload_bytes=200, bandwidth_mbps=10, duration_s=1,
                protocol="udp", server_ip="127.0.0.1", client_ip="127.0.0.1",
            )
        )
        reply = framed.read_message(timeout_s=5.0)
        framed.close()
        assert reply.type == "ERROR"
        assert reply.payload["code"] == "bad_handshake"
