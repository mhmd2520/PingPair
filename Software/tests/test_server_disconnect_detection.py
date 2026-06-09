"""Server-side mid-sweep disconnect detection (2026-06-04).

Symmetric follow-up to the Client link-watch. Three behaviours are pinned
here so the Server never again "keeps running normally until the full
sweep finishes" on a cable pull, and so an interrupted sweep ALWAYS raises
the Server's orange banner (never masquerades as a clean finish):

1. **Peer-close before FINISH → error event.** A client that vanishes
   without a clean FINISH means the sweep was cut short. The Server emits
   an ``error`` (→ banner) instead of a silent break that looked exactly
   like a clean run.
2. **Clean FINISH → sweep_finished, no error.** The happy path must stay
   quiet (regression guard for #1).
3. **Server link-watch → error event.** With the watched NIC's carrier
   down, the Server's message loop raises the disconnect within ~1 s
   instead of idling in ``read_message`` for the whole session.
4. **End-to-end: a mid-case link drop never produces a clean
   sweep_finished on the Server** — proves the Client's FINISH-guard
   (don't announce FINISH once the transport died) closes the
   flicker-back window that made the banner appear only intermittently.

Qt is not involved — these drive the headless ControlServer/ControlClient
over a real loopback socket.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from pingpair.config import load_default_config
from pingpair.core import fix_actions
from pingpair.core.case import CaseResult
from pingpair.core.control import client as client_module
from pingpair.core.control import server as server_module
from pingpair.core.control.client import ControlClient
from pingpair.core.control.protocol import FramedSocket, Message
from pingpair.core.control.server import ControlServer


# --- harness (replicated from test_round8_user_stop for self-containment) ---


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
    def __init__(self, *_a, **_k) -> None:
        pass

    def start(self) -> None:
        pass

    def wait(self, timeout_s: float | None = None) -> _FakeRunResult:
        return _FakeRunResult()

    def stop(self, *_a, **_k) -> None:
        pass


_FAKE_RUNNERS: list["_FakeCaseRunner"] = []


class _FakeCaseRunner:
    def __init__(self, cfg, case, *, loopback, on_line=None) -> None:
        self.case = case
        self._stop = threading.Event()
        self.run_started_at: float | None = None
        _FAKE_RUNNERS.append(self)

    def run(self) -> CaseResult:
        self.run_started_at = time.monotonic()
        deadline = self.run_started_at + 30.0
        while time.monotonic() < deadline:
            if self._stop.wait(0.1):
                break
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
        target=srv.serve_forever, kwargs={"bind_host": "127.0.0.1"}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
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


def _server_error_messages(events: list[tuple[str, dict]]) -> list[str]:
    return [d.get("message", "") for n, d in events if n == "error"]


# --- 1. peer-close before FINISH → error ------------------------------------


def test_peer_close_before_finish_emits_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    with _server_on(port, monkeypatch) as (_srv, _thread, events):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(Message.hello(client_version="test"))
        assert framed.read_message(timeout_s=5.0).type == "HELLO_OK"
        framed.write_message(Message.start_sweep(total_cases=4, sweep_id="x"))
        # Vanish WITHOUT a FINISH — an interrupted sweep.
        framed.close()
        time.sleep(0.6)

    names = [n for n, _ in events]
    assert "error" in names, f"interrupted sweep must emit an error; got {names}"
    assert "sweep_finished" not in names, (
        "an interrupted sweep must not look like a clean finish"
    )
    assert any(
        "finish" in m.lower() or "interrupted" in m.lower()
        for m in _server_error_messages(events)
    )


# --- 2. clean FINISH → sweep_finished, no error -----------------------------


def test_clean_finish_emits_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()
    with _server_on(port, monkeypatch) as (_srv, _thread, events):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(Message.hello(client_version="test"))
        assert framed.read_message(timeout_s=5.0).type == "HELLO_OK"
        framed.write_message(Message.start_sweep(total_cases=4, sweep_id="x"))
        framed.write_message(Message.finish())
        framed.close()
        time.sleep(0.6)

    names = [n for n, _ in events]
    assert "sweep_finished" in names, f"clean FINISH must finish cleanly; got {names}"
    assert "error" not in names, (
        f"a clean finish must not emit an error; got {_server_error_messages(events)!r}"
    )


# --- 3. server link-watch → error -------------------------------------------


def test_server_link_watch_emits_error_on_carrier_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force a watched adapter + a downed carrier.
    monkeypatch.setattr(
        ControlServer, "_resolve_link_adapter", lambda self, sock: "Ethernet"
    )
    monkeypatch.setattr(fix_actions, "adapter_link_up", lambda adapter: False)

    port = _free_port()
    with _server_on(port, monkeypatch) as (_srv, _thread, events):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(Message.hello(client_version="test"))
        # HELLO_OK is written before the message loop's first link check,
        # so we can still read it; the loop then trips the carrier watch.
        assert framed.read_message(timeout_s=5.0).type == "HELLO_OK"
        time.sleep(1.2)

    assert any(
        "link down" in m.lower() for m in _server_error_messages(events)
    ), f"carrier loss must raise a link-down error; got {_server_error_messages(events)!r}"


# --- 4. end-to-end: mid-case link drop never looks like a clean finish ------


def test_cable_pull_midsweep_never_clean_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Client's FINISH-guard must stop an aborted sweep from being
    reported as a clean finish on the Server (the intermittent-banner bug)."""
    _FAKE_RUNNERS.clear()
    port = _free_port()
    monkeypatch.setattr(client_module, "CaseRunner", _FakeCaseRunner)

    link = {"up": True}
    monkeypatch.setattr(fix_actions, "adapter_link_up", lambda adapter: link["up"])
    monkeypatch.setattr(ControlClient, "_resolve_link_adapter", lambda self: "Ethernet")
    # Server link-watch OFF here so we specifically exercise the FINISH-guard
    # + peer-close-before-FINISH path rather than the server carrier watch.
    monkeypatch.setattr(
        ControlServer, "_resolve_link_adapter", lambda self, sock: None
    )

    with _server_on(port, monkeypatch) as (_srv, _thread, server_events):
        cfg = load_default_config()
        cfg.network.control_port = port  # type: ignore[assignment]
        client_events: list[tuple[str, dict]] = []
        client = ControlClient(cfg, on_event=lambda n, d: client_events.append((n, d)))

        def _run() -> None:
            client.run_sweep(server_host="127.0.0.1", selected_indexes=[1, 2])

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _FAKE_RUNNERS and _FAKE_RUNNERS[-1].run_started_at is not None:
                break
            time.sleep(0.05)
        else:
            client.stop()
            t.join(timeout=5.0)
            pytest.fail("Client never entered runner.run()")

        time.sleep(0.5)       # clearly mid-case
        link["up"] = False    # PULL THE CABLE
        t.join(timeout=15.0)
        assert not t.is_alive(), "run_sweep hung after the link dropped"

    cnames = [n for n, _ in client_events]
    assert "error" in cnames
    assert any(
        "link down" in d.get("message", "").lower()
        for n, d in client_events
        if n == "error"
    )

    snames = [n for n, _ in server_events]
    assert "error" in snames, f"Server must report the interruption; got {snames}"
    assert "sweep_finished" not in snames, (
        "FINISH-guard failed: an aborted sweep was reported as a clean finish"
    )


# --- 5. out-of-order message mid-case → error + no clean finish --------------


def test_out_of_order_msg_in_case_emits_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a case the Server sends SERVER_READY then waits for CASE_DONE.
    A different message type arriving instead must surface an ``out-of-order``
    error and break the case wait (the runner is then torn down because the
    case did not complete cleanly) — never silently accepted nor a clean
    finish.
    """
    port = _free_port()
    with _server_on(port, monkeypatch) as (_srv, _thread, events):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(Message.hello(client_version="test"))
        assert framed.read_message(timeout_s=5.0).type == "HELLO_OK"
        framed.write_message(Message.start_sweep(total_cases=4, sweep_id="x"))
        framed.write_message(
            Message.start_case(
                case_idx=1, payload_bytes=200, bandwidth_mbps=10,
                duration_s=30, protocol="udp",
                server_ip="192.168.1.1", client_ip="192.168.1.2",
            )
        )
        assert framed.read_message(timeout_s=5.0).type == "SERVER_READY"
        # Out-of-order: a fresh START_SWEEP where CASE_DONE was expected.
        framed.write_message(Message.start_sweep(total_cases=4, sweep_id="y"))
        time.sleep(0.5)
        framed.close()
        time.sleep(0.3)

    msgs = _server_error_messages(events)
    assert any("out-of-order" in m.lower() for m in msgs), msgs
    assert "sweep_finished" not in [n for n, _ in events]


# --- 6. malformed START_CASE is reported, connection survives ---------------


def test_malformed_start_case_does_not_crash_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A START_CASE with a non-numeric field must be reported as malformed
    and skipped — not raise a KeyError/ValueError that escapes to the
    accept-loop catch-all and kills the connection with a raw message.
    The connection stays responsive afterwards.
    """
    port = _free_port()
    with _server_on(port, monkeypatch) as (_srv, _thread, events):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        framed = FramedSocket(sock)
        framed.write_message(Message.hello(client_version="test"))
        assert framed.read_message(timeout_s=5.0).type == "HELLO_OK"
        framed.write_message(Message.start_sweep(total_cases=4, sweep_id="x"))
        framed.write_message(
            Message(
                type="START_CASE",
                payload={
                    "case_idx": "not-a-number",
                    "payload_bytes": 200,
                    "bandwidth_mbps": 10,
                    "duration_s": 30,
                },
            )
        )
        time.sleep(0.4)
        # Still responsive: a HEARTBEAT is echoed back.
        framed.write_message(Message.heartbeat(ts=1.0))
        reply = framed.read_message(timeout_s=5.0)
        framed.close()
        time.sleep(0.2)

    assert reply.type == "HEARTBEAT", "connection must survive a bad START_CASE"
    assert any(
        "malformed start_case" in m.lower() for m in _server_error_messages(events)
    )
