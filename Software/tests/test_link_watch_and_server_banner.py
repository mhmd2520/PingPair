"""Mid-case physical-link watch + Server-side connection banner.

Two features land together here (2026-06-04):

1. **Cable-pull is caught in ~1 s, not at the end of the case.** A pulled
   Ethernet cable leaves the TCP control socket silently ESTABLISHED — no
   FIN/RST — so the existing ``select()`` socket monitor never fires for it
   and the disconnect only surfaced at the end-of-case CASE_DONE exchange
   (up to ~90 s later). The monitor now also polls the carrier of the NIC
   the connection rides (``adapter_link_up``), flips ``_link_down`` +
   ``_socket_alive`` and aborts iperf3 the moment the link drops.

2. **The Server raises the same cross-tab orange banner the Client does.**
   Previously a mid-sweep drop only updated the Server's in-panel status
   label; the omnipresent orange banner was Client-only. ``_ServerPanel``
   now feeds ``ctx.run_state.connection_warning_text`` on ``error`` and
   clears it on the next ``client_connected`` / ``sweep_starting`` /
   ``listening`` — but NOT on the benign ``client_disconnected`` that ends
   a clean sweep.
"""

from __future__ import annotations

import logging
import socket
import threading
from types import SimpleNamespace

import pytest

from pingpair.config import load_default_config
from pingpair.core import fix_actions
from pingpair.core.control.client import ControlClient
from pingpair.core.control.server import ControlServer


# ===========================================================================
# adapter_for_ipv4 — pure IP → adapter resolution
# ===========================================================================


def _fake_addrs(mapping: dict[str, list[str]]):
    """Build a psutil.net_if_addrs()-shaped dict: {iface: [snicaddr, ...]}."""
    out = {}
    for iface, ips in mapping.items():
        out[iface] = [
            SimpleNamespace(family=socket.AF_INET, address=ip) for ip in ips
        ]
    return out


def test_adapter_for_ipv4_matches_bound_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(
        psutil,
        "net_if_addrs",
        lambda: _fake_addrs({"Ethernet": ["192.168.1.2"], "Wi-Fi": ["10.0.0.5"]}),
    )
    assert fix_actions.adapter_for_ipv4("192.168.1.2") == "Ethernet"
    assert fix_actions.adapter_for_ipv4("10.0.0.5") == "Wi-Fi"


def test_adapter_for_ipv4_unknown_ip_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(
        psutil, "net_if_addrs", lambda: _fake_addrs({"Ethernet": ["192.168.1.2"]})
    )
    assert fix_actions.adapter_for_ipv4("192.168.1.99") is None


def test_adapter_for_ipv4_swallows_psutil_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psutil = pytest.importorskip("psutil")

    def _boom():
        raise OSError("psutil hiccup on an odd NIC")

    monkeypatch.setattr(psutil, "net_if_addrs", _boom)
    # Must degrade to None (→ caller skips link-watch) rather than raise.
    assert fix_actions.adapter_for_ipv4("192.168.1.2") is None


# ===========================================================================
# ControlClient._resolve_link_adapter
# ===========================================================================


def _client_with_local_ip(ip: str | None) -> ControlClient:
    client = ControlClient(load_default_config())
    if ip is None:
        client._sock = None
        return client

    class _FakeSock:
        def getsockname(self):
            return (ip, 5202)

    client._sock = SimpleNamespace(sock=_FakeSock())
    return client


def test_resolve_link_adapter_delegates_for_real_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fix_actions, "adapter_for_ipv4", lambda ip: f"NIC<{ip}>")
    client = _client_with_local_ip("192.168.1.2")
    assert client._resolve_link_adapter() == "NIC<192.168.1.2>"


def test_resolve_link_adapter_skips_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _spy(ip):
        called["n"] += 1
        return "should-not-be-used"

    monkeypatch.setattr(fix_actions, "adapter_for_ipv4", _spy)
    client = _client_with_local_ip("127.0.0.1")
    assert client._resolve_link_adapter() is None
    assert called["n"] == 0, "loopback must short-circuit before psutil lookup"


def test_resolve_link_adapter_no_socket_is_none() -> None:
    client = _client_with_local_ip(None)
    assert client._resolve_link_adapter() is None


# ===========================================================================
# ControlClient._monitor_control_socket — the link-down branch
# ===========================================================================


class _FakeRunner:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_monitor_flags_link_down_and_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down carrier on the watched NIC must abort the case immediately.

    The link check sits at the top of the monitor loop *before* select(),
    so a dummy socket object is never touched on this path.
    """
    monkeypatch.setattr(fix_actions, "adapter_link_up", lambda adapter: False)

    client = ControlClient(load_default_config())
    client._sock = SimpleNamespace(sock=object())  # non-None; never select'd
    runner = _FakeRunner()
    client._current_runner = runner

    client._monitor_control_socket(threading.Event(), "Ethernet")

    assert client._link_down is True
    assert client._socket_alive is False
    assert runner.stopped is True, "iperf3 runner must be torn down on link loss"


def test_monitor_ignores_link_when_no_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """link_adapter=None disables the carrier poll — no false cable-pull alarm.

    With the stop event pre-set the loop exits on its first guard, so we
    only assert that the link branch did not fire."""
    # adapter_link_up would say "down", but it must never be consulted.
    calls = {"n": 0}

    def _spy(adapter):
        calls["n"] += 1
        return False

    monkeypatch.setattr(fix_actions, "adapter_link_up", _spy)

    client = ControlClient(load_default_config())
    client._sock = SimpleNamespace(sock=object())
    stop = threading.Event()
    stop.set()  # exit immediately

    client._monitor_control_socket(stop, None)

    assert client._link_down is False
    assert client._socket_alive is True
    assert calls["n"] == 0


def test_monitor_flags_peer_eof_and_aborts() -> None:
    """A clean peer close (empty ``MSG_PEEK``) is the primary mid-test
    Server-kill signal: flip ``_socket_alive`` and tear down iperf3.

    Drives a real ``socket.socketpair``: closing the peer end makes the
    monitor's ``select`` report the socket readable, then ``recv(MSG_PEEK)``
    returns ``b""`` (EOF). No carrier poll (``link_adapter=None``).
    """
    a, b = socket.socketpair()
    try:
        client = ControlClient(load_default_config())
        client._sock = SimpleNamespace(sock=a)
        runner = _FakeRunner()
        client._current_runner = runner

        b.close()  # peer EOF
        client._monitor_control_socket(threading.Event(), None)

        assert client._socket_alive is False
        assert runner.stopped is True, "iperf3 runner must be torn down on EOF"
        assert client._link_down is False  # EOF is a socket drop, not a cable pull
    finally:
        a.close()


def test_monitor_flags_select_error_and_aborts() -> None:
    """``select`` raising (socket closed under the monitor) is treated as a
    disconnect, not swallowed — covers the ``except (OSError, ValueError)``
    branch."""
    a, b = socket.socketpair()
    client = ControlClient(load_default_config())
    client._sock = SimpleNamespace(sock=a)
    runner = _FakeRunner()
    client._current_runner = runner

    a.close()  # select([a], ...) now raises
    b.close()
    client._monitor_control_socket(threading.Event(), None)

    assert client._socket_alive is False
    assert runner.stopped is True


def test_monitor_keeps_polling_when_real_data_pending() -> None:
    """Pending control bytes (non-empty peek) are NOT a disconnect — the
    socket stays alive and the bytes are left for the main thread to read
    after iperf3. Guards the ``# Real data sitting in the buffer`` branch.
    """
    import time

    a, b = socket.socketpair()
    try:
        client = ControlClient(load_default_config())
        client._sock = SimpleNamespace(sock=a)
        runner = _FakeRunner()
        client._current_runner = runner

        b.sendall(b'{"type":"SERVER_RESULT"}\n')  # real data waiting, no close
        stop = threading.Event()
        t = threading.Thread(
            target=client._monitor_control_socket, args=(stop, None)
        )
        t.start()
        time.sleep(0.05)  # let it tick over the pending data a few times
        stop.set()
        t.join(timeout=2.0)

        assert not t.is_alive()
        assert client._socket_alive is True, "pending data must not trip a disconnect"
        assert runner.stopped is False
        # The monitor only peeked — the bytes are still there for the reader.
        assert b"SERVER_RESULT" in a.recv(64)
    finally:
        a.close()
        b.close()


# ===========================================================================
# ControlServer._resolve_link_adapter
# ===========================================================================


class _SockWithName:
    def __init__(self, ip: str) -> None:
        self._ip = ip

    def getsockname(self):
        return (self._ip, 5202)


def test_server_resolve_link_adapter_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fix_actions, "adapter_for_ipv4", lambda ip: f"NIC<{ip}>")
    srv = ControlServer(load_default_config())
    assert srv._resolve_link_adapter(_SockWithName("192.168.1.1")) == "NIC<192.168.1.1>"


def test_server_resolve_link_adapter_skips_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}
    monkeypatch.setattr(
        fix_actions, "adapter_for_ipv4", lambda ip: called.__setitem__("n", 1)
    )
    srv = ControlServer(load_default_config())
    assert srv._resolve_link_adapter(_SockWithName("127.0.0.1")) is None
    assert called["n"] == 0


# ===========================================================================
# _ServerPanel cross-tab connection banner (real widget, no listener thread)
# ===========================================================================

pytest.importorskip("PySide6", reason="server banner wiring needs Qt")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _build_server_panel(qapp, monkeypatch):
    from pingpair.context import AppContext, Role, RunState
    from pingpair.views import script_view

    monkeypatch.setattr(
        script_view._ServerPanel, "_start_server", lambda self: None
    )
    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-server-banner"),
        run_state=RunState(role=Role.SERVER),
    )
    return script_view._ServerPanel(ctx)


def test_server_error_sets_connection_banner(qapp, monkeypatch) -> None:
    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("error", {"message": "case 5: client disconnected"})
    assert panel.ctx.run_state.connection_warning_text == (
        "Client connection error: case 5: client disconnected"
    )


def test_server_banner_cleared_on_reconnect(qapp, monkeypatch) -> None:
    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("error", {"message": "case 5: client disconnected"})
    assert panel.ctx.run_state.connection_warning_text
    panel._on_event("client_connected", {"peer": "192.168.1.2:5050"})
    assert panel.ctx.run_state.connection_warning_text == ""


def test_server_banner_cleared_on_new_sweep(qapp, monkeypatch) -> None:
    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("error", {"message": "boom"})
    panel._on_event("sweep_starting", {"total_cases": 4})
    assert panel.ctx.run_state.connection_warning_text == ""


def test_server_banner_survives_disconnect(qapp, monkeypatch) -> None:
    """The error banner must persist through the client_disconnected that
    immediately follows a mid-sweep drop — otherwise it would clear itself
    instantly and the operator would never see it."""
    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("error", {"message": "case 5: client disconnected"})
    panel._on_event("client_disconnected", {})
    assert panel.ctx.run_state.connection_warning_text == (
        "Client connection error: case 5: client disconnected"
    )


def test_clean_sweep_leaves_no_banner(qapp, monkeypatch) -> None:
    """A normal sweep (no error) must never raise the banner."""
    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("listening", {"host": "192.168.1.1", "port": 5202})
    panel._on_event("client_connected", {"peer": "192.168.1.2:5050"})
    panel._on_event("sweep_starting", {"total_cases": 2})
    panel._on_event(
        "case_starting",
        {"case": "#01", "case_idx": 1, "position": 1, "total_cases": 2},
    )
    panel._on_event("case_done", {"case": "#01", "case_idx": 1, "returncode": 0})
    panel._on_event("sweep_finished", {"cases": 2, "total_cases": 2})
    panel._on_event("client_disconnected", {})
    assert panel.ctx.run_state.connection_warning_text == ""


def test_listener_label_tracks_new_sweep(qapp, monkeypatch) -> None:
    """The Listener line must not keep a previous run's 'Sweep finished'
    once a new sweep is running."""
    panel = _build_server_panel(qapp, monkeypatch)
    panel._on_event("listening", {"host": "192.168.1.1", "port": 5202})
    panel._on_event("client_connected", {"peer": "192.168.1.2:5050"})
    panel._on_event("sweep_starting", {"total_cases": 4})
    assert "in progress" in panel._status_label.text().lower()

    panel._on_event("sweep_finished", {"cases": 4, "total_cases": 4})
    assert panel._status_label.text() == "Sweep finished"

    # Second sweep on the same listener — the label must flip back to running,
    # not stay stuck on the prior run's "Sweep finished".
    panel._on_event("client_connected", {"peer": "192.168.1.2:5051"})
    panel._on_event("sweep_starting", {"total_cases": 4})
    assert "in progress" in panel._status_label.text().lower()
    assert "finished" not in panel._status_label.text().lower()
