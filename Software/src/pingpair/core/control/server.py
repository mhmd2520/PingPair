"""Server-side control channel.

Listens on ``cfg.network.control_port`` (TCP), accepts one Client at a
time, and obeys per-case ``START_CASE``/``CASE_DONE`` instructions by
spawning a fresh ``iperf3 -s -1`` subprocess for each case.

Qt-free: the ``on_event`` callback delivers every state transition so the
GUI can render a "case 7/20 in progress" line.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ... import __version__
from ...config import AppConfig
from .. import fix_actions
from ..plan import TestCase
from ..runner import ProcRunner, RunResult, iperf3_server_spec
from .protocol import (
    FramedSocket,
    Message,
    ProtocolError,
)

ServerEvent = Literal[
    "waiting_for_bind",
    "listening",
    "client_connected",
    "client_disconnected",
    "sweep_starting",
    "case_starting",
    "case_done",
    "sweep_finished",
    "error",
]
ServerEventCallback = Callable[[ServerEvent, dict], None]


@dataclass(slots=True)
class ServerSweepStats:
    """Lightweight summary the GUI can poll if it doesn't subscribe to events.

    ``total_cases`` is populated by the START_SWEEP message; it stays
    None on legacy clients that don't send one (and on the very first
    handshake before the message arrives).
    """

    cases_received: int = 0
    total_cases: int | None = None
    sweep_id: str = ""
    last_case_idx: int | None = None
    last_returncode: int | None = None

    def reset_for_new_sweep(self, *, total_cases: int, sweep_id: str) -> None:
        self.cases_received = 0
        self.total_cases = total_cases
        self.sweep_id = sweep_id
        self.last_case_idx = None
        self.last_returncode = None


class ControlServer:
    """Waits for a Client and obeys its case-by-case orchestration."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        on_event: ServerEventCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.stats = ServerSweepStats()
        self._listen_sock: socket.socket | None = None
        # Round-6 (Task X, 2026-05-13): track the *active client* socket
        # so ``stop()`` can close it and unblock any pending recv. The
        # previous version only closed the listen socket, so a handler
        # blocked on ``read_message(timeout_s=120.0)`` had no idea stop
        # had been requested and the GUI sat at "Listener: Stopping…"
        # for up to two minutes.
        self._client_sock: socket.socket | None = None
        self._stop = threading.Event()
        # Name of the NIC carrying the active client connection, resolved
        # once when a client connects (while the link is up). The case /
        # message loops poll its carrier so a point-to-point cable pull is
        # caught in ~1 s — a pulled cable leaves the TCP socket silently
        # ESTABLISHED, so the read loops alone would just time out for the
        # whole per-case deadline (~90 s). None = link-watch disabled.
        self._link_adapter: str | None = None

    # ------------------------------------------------------------------

    def serve_forever(self, *, bind_host: str | None = None) -> None:
        """Blocking listen-accept-handle loop. Returns when stop() is called."""
        host = bind_host or str(self.cfg.network.server_ip)
        port = self.cfg.network.control_port

        sock = self._bind_with_retry(host, port)
        if sock is None:
            # stop() was called before a bind ever succeeded — clean exit.
            return
        self._listen_sock = sock
        self._listen_sock.listen(1)
        self._emit("listening", {"host": host, "port": port})

        while not self._stop.is_set():
            try:
                client_sock, addr = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_client(client_sock, addr)
            except Exception as exc:  # noqa: BLE001 — never crash the server thread
                # Round-7 (Task EE, 2026-05-13): if stop() was called the
                # handler will see an OSError from the closed socket. That's
                # the expected shutdown path, not a real error — suppress the
                # misleading "error" event in that case.
                if not self._stop.is_set():
                    self._emit("error", {"message": str(exc)})
            finally:
                self._emit("client_disconnected", {})

        self._close_listen()

    # Poll interval while waiting for the configured IP to become
    # bindable. A bind attempt is a microsecond-cheap syscall, so a tight
    # 1 s cadence costs nothing and recovers within ~1 s of the IP
    # appearing — fast enough to feel instant after a Setup-tab IP fix.
    _BIND_RETRY_INTERVAL_S = 1.0

    def _bind_with_retry(self, host: str, port: int) -> socket.socket | None:
        """Bind a listen socket to ``host:port``, retrying until it works.

        The server IP is configured up-front, but on a DHCP/APIPA boot it
        may not be on any NIC yet — and right after the Setup-tab "Set the
        correct IP" fix, Windows can leave the freshly-set address in a
        brief *tentative* state where ``bind()`` still fails with WinError
        10049 ("address not valid"). The old code emitted a hard ``error``
        and the listener thread died, so the Client stayed unreachable on
        port 5202 until the app was restarted (Round-19 UU).

        Now we retry with a poll-friendly cadence, emitting a one-shot
        ``waiting_for_bind`` event so the GUI can show "waiting for
        <ip>…". The moment the IP becomes bindable — whether the user
        fixes it via the Setup tab or by hand — the next attempt succeeds
        and the listener comes up *in place*, no restart needed.

        Returns the bound socket, or ``None`` if :meth:`stop` was called
        before a bind succeeded.
        """
        warned = False
        while not self._stop.is_set():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.5)  # poll-friendly so stop() unblocks accept()
            try:
                sock.bind((host, port))
                return sock
            except OSError as exc:
                sock.close()
                if not warned:
                    # One-shot notice — don't spam the event log each tick.
                    self._emit(
                        "waiting_for_bind",
                        {"host": host, "port": port, "reason": str(exc)},
                    )
                    warned = True
                # Sleep the retry interval in 0.1 s ticks so a Stop click
                # (or app close) is honoured within ~0.1 s, not a full second.
                deadline = time.monotonic() + self._BIND_RETRY_INTERVAL_S
                while time.monotonic() < deadline:
                    if self._stop.is_set():
                        return None
                    time.sleep(0.1)
        return None

    def stop(self) -> None:
        self._stop.set()
        self._close_listen()
        self._close_client()

    # ------------------------------------------------------------------

    def _close_listen(self) -> None:
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except OSError:
                pass
            self._listen_sock = None

    def _close_client(self) -> None:
        """Shutdown + close the active client socket (if any).

        Called from :meth:`stop` so a handler blocked on ``recv`` wakes
        up immediately. Snapshots the socket reference first so a
        concurrent clear from ``_handle_client``'s finally block can't
        race us into ``NoneType.close``. (Task X, 2026-05-13.)
        """
        sock = self._client_sock
        self._client_sock = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _handle_client(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        # Stash the live client socket so ``stop()`` can shut it down
        # mid-blocked-recv (Task X). Always cleared in the finally
        # block so a clean exit doesn't leave a stale reference.
        self._client_sock = sock
        # Resolve the NIC behind this connection now, while the link is up,
        # so the loops below can watch its carrier for a mid-sweep cable pull.
        self._link_adapter = self._resolve_link_adapter(sock)
        framed = FramedSocket(sock)
        self._emit("client_connected", {"peer": f"{addr[0]}:{addr[1]}"})

        try:
            try:
                hello = framed.read_message(timeout_s=10.0)
            except ProtocolError as exc:
                try:
                    framed.write_message(
                        Message.error(code="hello_timeout", message=str(exc))
                    )
                except OSError:
                    pass
                return
            if hello.type != "HELLO":
                try:
                    framed.write_message(
                        Message.error(
                            code="bad_handshake",
                            message=f"expected HELLO, got {hello.type}",
                        )
                    )
                except OSError:
                    pass
                return

            framed.write_message(Message.hello_ok(server_version=__version__))

            # Tracks whether the client announced a clean FINISH. A peer
            # that vanishes *without* one means the sweep was interrupted
            # (cable pulled, client crash/abort) — surfaced as an error so
            # the Server raises the same banner the Client does, instead of
            # a silent break that looks indistinguishable from a clean run.
            got_finish = False

            # Main message loop. Round-6 (Task X): use a short polled
            # timeout so the loop re-checks ``_stop`` once per second
            # even when no client traffic is flowing. Together with the
            # ``_close_client()`` call from stop(), this guarantees the
            # handler exits within ~1 s of a Stop click instead of
            # blocking for the full 120 s read timeout.
            while not self._stop.is_set():
                # Carrier watch between cases: a cable pull here would
                # otherwise idle in read_message for the whole session.
                if self._link_adapter and not fix_actions.adapter_link_up(
                    self._link_adapter
                ):
                    self._emit(
                        "error",
                        {"message": "network link down — cable unplugged or NIC disabled"},
                    )
                    break
                try:
                    msg = framed.read_message(timeout_s=1.0)
                except ProtocolError as exc:
                    # Distinguish a read timeout (loop back and re-check
                    # _stop) from a real disconnect / peer-close (exit
                    # the loop). The previous version blocked for 120 s
                    # so the user saw "Listener: Stopping…" for ages.
                    if "timeout" in str(exc).lower() and not self._stop.is_set():
                        continue
                    # Non-timeout = peer closed / decode error. If no FINISH
                    # was seen, the sweep was cut short — raise the banner.
                    if not self._stop.is_set() and not got_finish:
                        self._emit(
                            "error",
                            {"message": "client disconnected before FINISH (sweep interrupted)"},
                        )
                    break

                if msg.type == "START_SWEEP":
                    # New-protocol notification: Client is starting a fresh
                    # sweep with N cases. Reset our per-sweep counter so the
                    # GUI title shows "case 1/N" instead of accumulating
                    # numbers across multiple sweeps in this listener's
                    # lifetime. ``total_cases`` defaults to 0 if the field
                    # is missing — back-compat with any future minimal
                    # client that forgets to populate it.
                    total_cases = int(msg.payload.get("total_cases", 0))
                    sweep_id = str(msg.payload.get("sweep_id", ""))
                    self.stats.reset_for_new_sweep(
                        total_cases=total_cases, sweep_id=sweep_id,
                    )
                    self._emit(
                        "sweep_starting",
                        {"total_cases": total_cases, "sweep_id": sweep_id},
                    )
                elif msg.type == "START_CASE":
                    self._on_start_case(framed, msg)
                elif msg.type == "CASE_DONE":
                    # Already handled inside _on_start_case (we wait for it there).
                    # Receiving an extra CASE_DONE here is benign.
                    continue
                elif msg.type == "FINISH":
                    got_finish = True
                    self._emit(
                        "sweep_finished",
                        {
                            "cases": self.stats.cases_received,
                            "total_cases": self.stats.total_cases,
                        },
                    )
                    break
                elif msg.type == "HEARTBEAT":
                    try:
                        framed.write_message(
                            Message.heartbeat(ts=msg.payload.get("ts", 0))
                        )
                    except OSError:
                        break
                else:
                    # Unknown — log via event but stay alive.
                    self._emit("error", {"message": f"unexpected msg type {msg.type}"})
        finally:
            # Always tear down the client socket reference so the next
            # ``stop()`` doesn't try to close a dead socket. (Task X.)
            try:
                framed.close()
            except Exception:  # noqa: BLE001
                pass
            self._client_sock = None
            self._link_adapter = None

    def _resolve_link_adapter(self, sock: socket.socket) -> str | None:
        """NIC behind this client connection — for the mid-sweep link watch.

        Thin wrapper over the shared
        :func:`core.fix_actions.adapter_for_socket` (which also serves the
        Client side): resolves the accepted socket's local IPv4 (the Server's
        bound IP) to an adapter name so the case / message loops can poll its
        carrier. Returns ``None`` (→ loops skip the link watch) for a loopback
        bind / unresolvable socket. Best-effort; never raises.
        """
        return fix_actions.adapter_for_socket(sock)

    def _on_start_case(self, framed: FramedSocket, msg: Message) -> None:
        p = msg.payload
        # Defensive parse: a malformed START_CASE (missing or non-numeric
        # field) from a buggy or hostile peer must not raise a KeyError /
        # ValueError that escapes to the accept-loop catch-all and kills the
        # connection with a raw message. Treat it as a protocol error for
        # THIS case and bail cleanly — mirrors the guarded CASE_DONE read
        # below and the client-side payload guards.
        try:
            case = TestCase(
                index=int(p["case_idx"]),
                payload_bytes=int(p["payload_bytes"]),
                bandwidth_mbps=int(p["bandwidth_mbps"]),
                duration_s=int(p["duration_s"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._emit("error", {"message": f"malformed START_CASE ignored: {exc}"})
            return
        # cases_received hasn't incremented yet — that happens at case_done.
        # For the GUI title's "M/N" the right "M" is "the case about to
        # start" = cases_received + 1.
        position = self.stats.cases_received + 1
        self._emit(
            "case_starting",
            {
                "case": case.label,
                "case_idx": case.index,
                "position": position,
                "total_cases": self.stats.total_cases,
            },
        )

        # Fresh iperf3 -s -1 per case so port state is clean.
        spec = iperf3_server_spec(self.cfg, json=True)
        runner = ProcRunner(spec)
        runner.start()

        # Tell the Client we're listening so it can fire its iperf3 -c.
        # Note: a tiny warmup is built into the Client's wait, so we don't
        # have to sleep here.
        try:
            framed.write_message(Message.server_ready(case_idx=case.index, pid=0))
        except OSError as exc:
            # Round-6 (Task X): Client dropped before we could announce
            # readiness. Tear down the iperf3 server and bail; the
            # _handle_client loop will see the disconnect on its next
            # read and exit cleanly.
            self._emit("error", {"message": f"case {case.index}: {exc}"})
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                pass
            return

        # Wait either for the case to finish naturally (-1 makes iperf3 exit
        # when the client disconnects) or for an explicit CASE_DONE.
        # The Client always sends CASE_DONE after its iperf3-client returns.
        # Round-6 (Task X): poll in 1 s ticks so a Stop click is honored
        # within ~1 s instead of waiting for the full duration_s*2+30
        # timeout (~90 s for a 30 s case). We bail when ``_stop`` is set
        # OR the deadline expires OR the framed read raises.
        case_deadline = (
            time.monotonic() + case.duration_s * 2 + 30.0
        )
        client_done = False
        try:
            while not client_done and not self._stop.is_set():
                # Mid-case carrier watch: a pulled cable sends no FIN/RST, so
                # read_message below would just keep timing out until the
                # ~90 s deadline. Polling the NIC catches it within ~1 s; the
                # raise routes into the ProtocolError handler → error event →
                # banner, and tears down this case's iperf3 -s below.
                if self._link_adapter and not fix_actions.adapter_link_up(
                    self._link_adapter
                ):
                    raise ProtocolError(
                        f"case {case.index}: network link down — "
                        "cable unplugged or NIC disabled"
                    )
                remaining = case_deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError(
                        f"case {case.index}: timed out waiting for CASE_DONE"
                    )
                try:
                    next_msg = framed.read_message(
                        timeout_s=min(1.0, remaining)
                    )
                except ProtocolError as exc:
                    if "timeout" in str(exc).lower():
                        # Normal poll-loop tick — re-check _stop + deadline.
                        continue
                    raise  # disconnect / decode error → break out below
                # Guard the payload access: a malformed CASE_DONE (missing /
                # non-numeric case_idx) must not raise a KeyError/ValueError
                # here — that would escape the `except ProtocolError` and leak
                # the iperf3 -s runner. Treat it as out-of-order instead.
                done_idx = next_msg.payload.get("case_idx")
                try:
                    done_idx = int(done_idx) if done_idx is not None else None
                except (TypeError, ValueError):
                    done_idx = None
                if next_msg.type == "CASE_DONE" and done_idx == case.index:
                    client_done = True
                elif next_msg.type == "HEARTBEAT":
                    continue
                else:
                    self._emit("error", {"message": f"out-of-order msg {next_msg.type}"})
                    break
        except ProtocolError as exc:
            err_text = str(exc)
            if "peer closed" in err_text.lower() or "stopped by user" in err_text.lower():
                # Round-8 (Task HH, 2026-05-13): client disconnected
                # mid-case - could be a user-stop on the Client, an app
                # close, or a real crash. Softer wording than the raw
                # protocol error so the Server log doesn't scream.
                self._emit(
                    "error",
                    {"message": f"case {case.index}: client disconnected"},
                )
            else:
                self._emit("error", {"message": f"case {case.index}: {exc}"})

        # Now drain iperf3 server output (it should already have exited).
        # Wind the runner down whenever the case did NOT finish cleanly:
        # a user stop, a mid-case client disconnect, an out-of-order msg, or
        # a deadline timeout all leave ``client_done`` False. Without this,
        # the disconnect/timeout path fell straight through to the 10 s
        # ``wait()``, stalling the server thread for 10 s and pinning the
        # orphaned iperf3 -s subprocess for that whole window on every drop.
        if self._stop.is_set() or not client_done:
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                pass
        result: RunResult = runner.wait(timeout_s=10.0)

        try:
            framed.write_message(
                Message.server_result(
                    case_idx=case.index,
                    iperf3_json=result.stdout,
                    returncode=result.returncode,
                )
            )
        except OSError as exc:
            # Client closed before we could ship the result. Surface it
            # so the GUI's event log shows the disconnect, then return
            # — the _handle_client loop will exit on its next read.
            self._emit(
                "error",
                {
                    "message": (
                        f"case {case.index}: client disconnected before "
                        f"SERVER_RESULT could be sent ({exc})"
                    ),
                },
            )
            return

        self.stats.cases_received += 1
        self.stats.last_case_idx = case.index
        self.stats.last_returncode = result.returncode
        self._emit(
            "case_done",
            {
                "case": case.label,
                "case_idx": case.index,
                "returncode": result.returncode,
                "cases_received": self.stats.cases_received,
                "total_cases": self.stats.total_cases,
            },
        )

    def _emit(self, event: ServerEvent, data: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, data)
            except Exception:  # noqa: BLE001
                pass
