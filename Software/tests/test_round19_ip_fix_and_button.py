"""Round-19 VM-test fixes — UU (Server listener self-heals when its IP
isn't bindable yet) and VV (Run button re-enable after a zero-case abort).

Both bugs were reported by Mohamed after running the app on two VMs that
booted on DHCP/APIPA (no DHCP server on the isolated Ethernet, so each
NIC self-assigned 169.254.x.x).

**UU — Server unreachable after the Fix button, until app restart.**
The Server binds its control listener to ``cfg.network.server_ip`` at
launch (mirroring iperf3's ``-B`` bind). On a DHCP/APIPA boot that IP
isn't on any NIC, so ``bind()`` fails with WinError 10049. The *first*
fix attempt — rebuilding the Run tab right after the Setup-tab "Set the
correct IP" netsh call — did NOT work: Windows leaves a freshly-set
static IP in a brief *tentative* state where ``bind()`` still fails, so
the rebuilt listener died again. Closing + reopening worked only because
of the human delay before the next launch.

The shipped fix makes the listener *self-heal*: ``serve_forever`` now
retries the bind on a 1 s cadence (emitting a one-shot ``waiting_for_bind``
event) instead of emitting a hard ``error`` and dying. The moment the IP
becomes bindable — via the Fix button or a manual change — the next
attempt succeeds and the listener comes up *in place*, no restart.

**VV — "Run subset" button stuck disabled after a connection failure.**
``_start_sweep_worker`` disables the Run button at sweep start; every
clean-finish path re-enables it via ``_on_selection_changed``, but the
abort / user-stop / partial-stop paths route through
``_reset_after_zero_case_finish``, which skipped that call — so after a
failure the button stayed greyed until the user toggled a row. Fix: call
``_on_selection_changed`` from ``_reset_after_zero_case_finish``.

The UU tests are Qt-free (they drive the real ``ControlServer`` retry
loop). The VV tests build a real ``_ClientPanel`` under the offscreen Qt
platform (see ``conftest.py``) and exercise the actual fixed slot.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import pytest

from pingpair.config import load_default_config
from pingpair.context import AppContext, RunState, Role
from pingpair.core.control.server import ControlServer


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ===========================================================================
# UU — the listener self-heals instead of dying on an unbindable IP.
# ===========================================================================


def test_listener_waits_for_absent_ip_instead_of_dying() -> None:
    """A bind to an IP not on this host must NOT kill the listener.

    192.0.2.1 (TEST-NET-1, RFC 5737) is reserved and never assigned to a
    real NIC, so ``bind()`` fails with EADDRNOTAVAIL / WSAEADDRNOTAVAIL on
    every platform — exactly the DHCP-launch condition. The listener must
    stay alive (retrying), emit ``waiting_for_bind`` (not ``error`` or
    ``listening``), and still honour ``stop()`` promptly while waiting.
    """
    cfg = load_default_config()
    cfg.network.control_port = _free_port()  # type: ignore[assignment]

    events: list[tuple[str, dict]] = []
    srv = ControlServer(cfg, on_event=lambda n, d: events.append((n, d)))
    thread = threading.Thread(
        target=srv.serve_forever,
        kwargs={"bind_host": "192.0.2.1"},
        daemon=True,
    )
    thread.start()

    # Give it a couple of retry cycles, then prove it's still going.
    time.sleep(1.5)
    try:
        assert thread.is_alive(), (
            "listener thread must keep retrying, not die, when the IP "
            "isn't bindable yet (the UU regression)."
        )
        names = [n for n, _ in events]
        assert "waiting_for_bind" in names, (
            f"expected a 'waiting_for_bind' notice; got {names}"
        )
        assert "listening" not in names, "must not claim it's listening"
        assert "error" not in names, (
            "must NOT emit a hard 'error' — that path killed the listener"
        )
    finally:
        stop_at = time.monotonic()
        srv.stop()
        thread.join(timeout=3.0)

    assert not thread.is_alive(), "stop() must end the retry loop"
    assert time.monotonic() - stop_at < 2.0, (
        "stop() must be honoured within ~1 retry tick while waiting."
    )


def test_listener_retries_until_bind_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the IP becomes bindable, the next retry brings the listener up.

    Simulates the tentative-IP window by failing ``bind()`` twice (as
    Windows does right after a static-IP change) before letting it
    succeed on 127.0.0.1 — proving the in-place recovery the UU fix
    relies on. A real client connection confirms the port is truly live.
    """
    port = _free_port()
    cfg = load_default_config()
    cfg.network.control_port = port  # type: ignore[assignment]

    real_bind = socket.socket.bind
    attempts = {"n": 0}

    def flaky_bind(self, address):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise OSError(10049, "WSAEADDRNOTAVAIL (simulated tentative IP)")
        return real_bind(self, address)

    monkeypatch.setattr(socket.socket, "bind", flaky_bind)

    events: list[tuple[str, dict]] = []
    srv = ControlServer(cfg, on_event=lambda n, d: events.append((n, d)))
    thread = threading.Thread(
        target=srv.serve_forever,
        kwargs={"bind_host": "127.0.0.1"},
        daemon=True,
    )
    thread.start()

    # Wait for the listener to come up on a later retry.
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if any(n == "listening" for n, _ in events):
            break
        time.sleep(0.05)

    try:
        names = [n for n, _ in events]
        assert "waiting_for_bind" in names, (
            f"expected it to wait through the failed binds; got {names}"
        )
        assert "listening" in names, (
            f"expected it to bind once the IP went live; got {names}"
        )
        assert attempts["n"] >= 3, (
            f"expected >2 bind attempts (2 fail + 1 ok), got {attempts['n']}"
        )
        # Connection proves the socket is genuinely listening (connect()
        # doesn't call bind(), so the flaky patch doesn't affect it).
        conn = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        conn.close()
    finally:
        srv.stop()
        thread.join(timeout=3.0)


# ===========================================================================
# VV — Run button re-enables after a zero-case abort (real Qt panel).
# ===========================================================================
#
# The guards live in the fixture (not at module level) so the Qt-free UU
# tests above still run on a sandbox without PySide6 / pyqtgraph.


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="VV needs a Qt widget")
    pytest.importorskip("pyqtgraph", reason="_ClientPanel builds pyqtgraph plots")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def _build_client_panel(qapp, *, role: Role = Role.CLIENT, loopback: bool = False):
    """Construct a real _ClientPanel against a minimal AppContext.

    A bare ``RunState`` (no QSettings round-trip) is enough — the panel
    only reads ``selected_case_indexes`` / ``continuous_mode`` /
    ``server_host_override`` and the default ``AppConfig``.
    """
    from pingpair.views.script_view import _ClientPanel

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round19"),
        run_state=RunState(role=role),
    )
    return _ClientPanel(ctx, loopback=loopback)


def test_run_button_reenabled_after_zero_case_abort(qapp) -> None:
    """Full-sweep selection: a 0-case abort must leave Run clickable."""
    panel = _build_client_panel(qapp)

    # Mirror the state left by _start_sweep_worker (button disabled) and
    # _on_sweep_finished (worker cleared) just before the abort path runs.
    panel._run_btn.setEnabled(False)
    panel._active_subset = [1]
    panel._worker = None

    panel._reset_after_zero_case_finish()

    assert panel._run_btn.isEnabled(), (
        "Run button must be re-enabled after a zero-case abort — it was "
        "left disabled (the VV regression)."
    )
    assert panel._run_btn.text() == "Run full sweep"


def test_run_button_reenabled_after_abort_with_subset(qapp) -> None:
    """Subset selection: button re-enables AND keeps the subset label."""
    panel = _build_client_panel(qapp)
    panel._sweep_table.set_selected_case_indexes([1, 2, 3])

    panel._run_btn.setEnabled(False)
    panel._active_subset = [1, 2, 3]
    panel._worker = None

    panel._reset_after_zero_case_finish()

    assert panel._run_btn.isEnabled(), (
        "Run button must re-enable after a subset sweep aborts."
    )
    assert panel._run_btn.text() == "Run subset (3 cases)"


def test_run_button_stays_disabled_when_nothing_selected(qapp) -> None:
    """Guard: with no cases selected the button correctly stays disabled.

    The re-enable is driven by _on_selection_changed, which disables the
    button at selected==0 — so the abort reset must not blindly enable it.
    """
    panel = _build_client_panel(qapp)
    panel._sweep_table.select_none()

    panel._run_btn.setEnabled(False)
    panel._active_subset = []
    panel._worker = None

    panel._reset_after_zero_case_finish()

    assert not panel._run_btn.isEnabled(), (
        "With zero cases selected the Run button must remain disabled."
    )


# ---------------------------------------------------------------------------
# Fix-all ordering: disable Wi-Fi BEFORE assigning the static IP
#
# On a host whose Wi-Fi shares the test subnet (Wi-Fi on 192.168.1.x), "Fix
# all" used to set the Ethernet static 192.168.1.2 while Wi-Fi still owned the
# subnet -> Windows dropped the static to APIPA (netsh rc=0, so it reported
# "succeeded" but the IP never bound and pings failed). The fix orders
# subnet-clearing fixes before IP assignment.
# ---------------------------------------------------------------------------


def test_fix_all_disables_wifi_before_setting_static_ip() -> None:
    from pingpair.views.setup_view import order_fix_ids

    # run_checks emits the IP fix (set_static_ip) BEFORE the Wi-Fi fix
    # (disable_wifi); order_fix_ids must flip that so the subnet is free when
    # the IP is assigned.
    raw = ["set_static_ip", "disable_wifi", "open_icmp", "open_iperf3_ports"]
    ordered = order_fix_ids(raw)
    assert ordered.index("disable_wifi") < ordered.index("set_static_ip")
    # Firewall fixes keep their original relative order, after the IP set.
    assert ordered.index("set_static_ip") < ordered.index("open_icmp")
    assert ordered.index("open_icmp") < ordered.index("open_iperf3_ports")


def test_order_fix_ids_is_stable_for_unlisted() -> None:
    from pingpair.views.setup_view import order_fix_ids

    # Only Wi-Fi/IP are reordered; everything else keeps input order.
    raw = ["open_control_port", "open_icmp", "open_iperf3_udp"]
    assert order_fix_ids(raw) == raw


def test_order_fix_ids_wifi_first_even_if_alone_with_ip() -> None:
    from pingpair.views.setup_view import order_fix_ids

    assert order_fix_ids(["set_static_ip", "disable_wifi"]) == [
        "disable_wifi", "set_static_ip",
    ]


# ---------------------------------------------------------------------------
# Fix-outcome dialog: surface the reason in the main text, drop redundant
# Details for single-line failures (2026-06-03 follow-up).
# ---------------------------------------------------------------------------


def _fr(ok=False, stdout="", stderr="", rc=-1):
    from pingpair.core.fix_actions import FixResult
    return FixResult(ok=ok, stdout=stdout, stderr=stderr, returncode=rc)


def test_summarize_fix_error_cable_message() -> None:
    from pingpair.views.setup_view import _summarize_fix_error

    msg = 'Ethernet cable unplugged on "Ethernet" (media disconnected). Connect it.'
    assert _summarize_fix_error(_fr(stderr=msg)) == msg


def test_summarize_fix_error_skips_transcript_scaffolding() -> None:
    from pingpair.views.setup_view import _summarize_fix_error

    # A real netsh failure: stderr is all transcript headers/blank, the actual
    # error is in stdout — the summary must surface the error line, not "--- ".
    stdout = (
        "--- attempt 1 (static) ---\n"
        "The requested operation requires elevation.\n"
        "--- DHCP release ---\n(empty)\n"
        "--- attempt 2 (static after release) ---\n(empty)"
    )
    stderr = "--- attempt 1 (static) ---\n\n--- DHCP release ---\n\n--- attempt 2 ---\n"
    assert _summarize_fix_error(_fr(stdout=stdout, stderr=stderr)) == (
        "The requested operation requires elevation."
    )


def test_summarize_fix_error_falls_back_when_empty() -> None:
    from pingpair.views.setup_view import _summarize_fix_error

    assert _summarize_fix_error(_fr()) == "See the details for the command output."


def test_fix_details_useful_false_for_single_line_failure() -> None:
    from pingpair.views.setup_view import _fix_details_useful

    # Cable refusal: empty stdout + one stderr line -> Details adds nothing.
    assert _fix_details_useful(_fr(stderr="Ethernet cable unplugged.")) is False
    # Clean success: no output -> no Details.
    assert _fix_details_useful(_fr(ok=True, rc=0)) is False


def test_fix_details_useful_true_for_netsh_transcript() -> None:
    from pingpair.views.setup_view import _fix_details_useful

    # Multi-attempt netsh transcript in stdout -> Details adds value.
    assert _fix_details_useful(
        _fr(stdout="--- attempt 1 (static) ---\nThe parameter is incorrect.")
    ) is True
