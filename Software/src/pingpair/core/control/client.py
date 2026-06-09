"""Client-side control channel driver.

Connects to the Server (laptop A), walks the test plan from
:func:`core.plan.build_plan`, and per case:

1. Send ``START_CASE`` with the per-case parameters.
2. Wait ``SERVER_READY``.
3. Spawn local iperf3-client + fping in parallel.
4. When the iperf3-client finishes, send ``CASE_DONE``.
5. Read ``SERVER_RESULT`` to capture the server-side iperf3 JSON.
6. Append to a :class:`SweepResult`.

When all cases are done, send ``FINISH`` and close.

This module is Qt-free.  The Run tab wraps it in a QThread.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ... import __version__
from ...config import AppConfig
from ..case import CaseResult, CaseRunner
from ..plan import TestCase, build_plan
from .protocol import (
    FramedSocket,
    Message,
    ProtocolError,
)

ClientEvent = Literal[
    "connecting",
    "connected",
    "case_starting",
    "case_done",
    "sweep_finished",
    "error",
    "disconnected",
]
ClientEventCallback = Callable[[ClientEvent, dict], None]
LineCallback = Callable[[str, str], None]   # for live log


@dataclass(slots=True)
class SweepCaseEntry:
    """One row of the sweep table."""

    case: TestCase
    case_result: CaseResult | None = None
    server_iperf3_json: str = ""
    server_returncode: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.case_result is not None and self.case_result.ok and self.error is None


@dataclass(slots=True)
class SweepResult:
    """Aggregated outcome of one full Run."""

    started_at: float
    ended_at: float = 0.0
    cases: list[SweepCaseEntry] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at if self.ended_at else 0.0


# ---------------------------------------------------------------------------
# Group C-1: multi-segment runs
# ---------------------------------------------------------------------------
#
# A "segment" is one full sweep against the Server (HELLO → START_SWEEP →
# cases → FINISH). The Server doesn't know it's part of a larger workflow;
# the multi-segment loop is orchestrated entirely on the Client side. This
# matches the train use case where the operator physically unplugs from
# one car-pair and plugs into the next between segments.


SegmentStatus = Literal["ok", "partial", "failed"]


@dataclass(slots=True)
class SweepSegment:
    """One segment in a multi-segment run.

    ``status`` reflects the outcome of *this* segment only:

    * **ok** — the segment ran to completion and every case is ``entry.ok``.
    * **partial** — the segment ran to completion but at least one case
      errored (e.g. iperf3 returncode != 0 for that case).
    * **failed** — the segment couldn't complete (TCP drop mid-sweep,
      operator pulled the cable, control-channel timeout). The ``sweep``
      will still hold whatever cases finished cleanly before the failure.
    """

    segment_idx: int                   # 1-based, matches display order
    label: str                         # operator-supplied, e.g. "Cab M2 ↔ M4"
    sweep: SweepResult                 # full per-case data for this segment
    status: SegmentStatus = "ok"
    error: str = ""                    # human-readable detail when not "ok"

    @property
    def cases_ok(self) -> int:
        return sum(1 for e in self.sweep.cases if e.ok)

    @property
    def cases_total(self) -> int:
        return len(self.sweep.cases)


@dataclass(slots=True)
class MultiSweepResult:
    """Aggregated outcome of one multi-segment run.

    The ``selected_case_indexes`` field is shared across all segments
    (subset scope decision 2026-05-11). Each segment's ``sweep`` reruns
    the same filtered plan, so all segments characterise the same
    metric grid — required for the cross-segment comparison table in
    the report.
    """

    started_at: float
    ended_at: float = 0.0
    segments: list[SweepSegment] = field(default_factory=list)
    selected_case_indexes: list[int] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at if self.ended_at else 0.0

    @property
    def segments_ok(self) -> int:
        return sum(1 for s in self.segments if s.status == "ok")

    @property
    def segments_total(self) -> int:
        return len(self.segments)

    @property
    def total_cases(self) -> int:
        return sum(s.cases_total for s in self.segments)

    @property
    def total_cases_ok(self) -> int:
        return sum(s.cases_ok for s in self.segments)


class ControlClient:
    """Drives a sweep against a remote (or loopback) Server."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        on_event: ClientEventCallback | None = None,
        on_line: LineCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.on_line = on_line
        self._sock: FramedSocket | None = None
        self._stop = False
        self._current_runner: CaseRunner | None = None
        # Round-6 (Task W, 2026-05-13): track whether the control socket
        # is still usable. Flipped to False the moment any per-case I/O
        # raises OSError (e.g. ConnectionResetError when the Server is
        # killed mid-test) so :meth:`run_sweep` can break the case loop
        # immediately instead of trying to write CASE_DONE to a dead
        # socket on every subsequent case.
        self._socket_alive = True
        # Set True by the mid-case link monitor when the NIC carrying the
        # control connection loses its carrier (cable pulled / adapter
        # disabled). Lets the post-case code surface a precise "cable
        # unplugged" message instead of the generic "Server disconnected".
        self._link_down = False

    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Cooperative abort. Safe across threads."""
        self._stop = True
        if self._current_runner is not None:
            self._current_runner.stop()
        # Round-6: close the control socket so any blocked recv/send
        # returns immediately. Without this, pressing Stop while the
        # Client was waiting on SERVER_RESULT could leave the worker
        # blocked for up to ~90 s (the inner read timeout).
        #
        # Snapshot the reference first (like the server's _close_client):
        # stop() runs on the Qt thread while the sweep worker thread's
        # ``finally`` may null ``self._sock`` concurrently. A bare
        # ``if self._sock is not None: self._sock.close()`` could see the
        # attribute cleared between the check and the call (TOCTOU) and
        # raise AttributeError — currently swallowed, but a real race.
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    def run_sweep(
        self,
        *,
        server_host: str | None = None,
        selected_indexes: list[int] | None = None,
        loopback: bool = False,
    ) -> SweepResult:
        """Connect, walk the plan, return the aggregated SweepResult.

        ``selected_indexes`` (Group B) lets the caller restrict the run
        to a subset of the canonical 20 cases. ``None`` or an empty
        list means "run all cases" (back-compat with Phase 3b callers).
        Indexes are filtered against the actual plan, so a stale value
        in QSettings can't smuggle in a non-existent case.

        ``loopback`` (Round 18 — PP) runs the whole sweep on 127.0.0.1
        with no control channel: each case is a local
        ``CaseRunner(loopback=True)`` that spawns both iperf3 ends
        itself. The emitted event stream is identical to the
        two-machine path, so the same GUI sweep panel drives both.
        """
        host = server_host or str(self.cfg.network.server_ip)
        port = self.cfg.network.control_port
        plan = build_plan(self.cfg)

        if selected_indexes:
            wanted = set(selected_indexes)
            plan = [c for c in plan if c.index in wanted]

        sweep = SweepResult(started_at=time.time())
        # Round-6: every sweep starts with a fresh socket-alive flag so
        # a previous sweep's mid-flight disconnect doesn't poison the
        # next one (relevant in continuous mode where the same
        # ControlClient instance is reused).
        self._socket_alive = True
        self._link_down = False

        if loopback:
            return self._run_sweep_loopback(plan, sweep)

        self._emit("connecting", {"host": host, "port": port})
        try:
            self._sock = self._connect_with_retry(host, port)
        except ProtocolError as exc:
            self._emit("error", {"message": str(exc)})
            sweep.ended_at = time.time()
            self._emit("sweep_finished", {"cases": len(sweep.cases)})
            return sweep

        try:
            self._sock.write_message(
                Message.hello(client_version=__version__)
            )
            ack = self._sock.read_message(timeout_s=10.0)
            if ack.type != "HELLO_OK":
                raise ProtocolError(f"expected HELLO_OK, got {ack.type}")
            self._emit("connected", {"server_version": ack.payload.get("server_version")})

            # Announce the sweep so the Server can reset its per-sweep
            # counter and learn the total. One-way notification — no ack.
            self._sock.write_message(
                Message.start_sweep(
                    total_cases=len(plan),
                    sweep_id=f"{sweep.started_at:.0f}",
                )
            )

            for case in plan:
                if self._stop:
                    break
                entry = self._run_one_case(case)
                sweep.cases.append(entry)
                self._emit(
                    "case_done",
                    {
                        "case": case.label,
                        "case_idx": case.index,
                        "ok": entry.ok,
                        "entry": entry,
                    },
                )
                # Round-6 (Task W): bail out of the case loop the moment
                # the control socket dies, otherwise every subsequent
                # case will hit the same OSError when it tries to write
                # START_CASE and the user waits N x timeout for the sweep
                # to "finish".
                if self._stop:
                    # Round-8 (Task FF, 2026-05-13): user-initiated stop.
                    # No error event — clean break.
                    break
                if not self._socket_alive:
                    self._emit(
                        "error",
                        {
                            "message": entry.error
                            or "Server connection lost mid-sweep",
                        },
                    )
                    break

            # Only announce a clean FINISH when the transport is still
            # healthy. If a case aborted because the link/socket died
            # (``_socket_alive`` False — e.g. the cable was pulled), sending
            # FINISH would be a lie: should the link flicker back just as
            # this runs, the Server receives FINISH and reports the sweep as
            # *cleanly finished* (no error, no banner) even though it was
            # interrupted. Skipping it lets the Server detect the drop
            # (link-watch / peer-close-before-FINISH) and raise its banner.
            if self._socket_alive:
                try:
                    self._sock.write_message(Message.finish())
                except (OSError, ProtocolError):
                    pass

        except (OSError, ProtocolError) as exc:
            # Round-6 (Task W): catch HELLO/HELLO_OK/START_SWEEP failures
            # and any other unexpected I/O error from the inner block.
            # Round-8 (Task FF, 2026-05-13): suppress the error event when
            # the user just pressed Stop — the closed socket is expected.
            if not self._stop:
                self._emit("error", {"message": str(exc)})
        finally:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:  # noqa: BLE001
                    pass
            self._sock = None

        sweep.ended_at = time.time()
        self._emit("sweep_finished", {"cases": len(sweep.cases)})
        return sweep

    # ------------------------------------------------------------------

    def _run_sweep_loopback(
        self, plan: list[TestCase], sweep: SweepResult
    ) -> SweepResult:
        """Loopback sweep — walk ``plan`` on 127.0.0.1, no control channel.

        Each case is a local ``CaseRunner(loopback=True)`` (it spawns
        the iperf3 server, the iperf3 client, and fping itself). The
        events emitted here match :meth:`run_sweep`'s two-machine path
        so the same GUI sweep panel drives both.
        """
        for case in plan:
            if self._stop:
                break
            entry = self._run_one_case_loopback(case)
            sweep.cases.append(entry)
            self._emit(
                "case_done",
                {
                    "case": case.label,
                    "case_idx": case.index,
                    "ok": entry.ok,
                    "entry": entry,
                },
            )
            if self._stop:
                break
        sweep.ended_at = time.time()
        self._emit("sweep_finished", {"cases": len(sweep.cases)})
        return sweep

    def _run_one_case_loopback(self, case: TestCase) -> SweepCaseEntry:
        """Run one case locally on 127.0.0.1 — no server, no socket."""
        entry = SweepCaseEntry(case=case)
        self._emit(
            "case_starting", {"case": case.label, "case_idx": case.index}
        )
        if self._stop:
            entry.error = "stopped by user"
            return entry
        runner = CaseRunner(self.cfg, case, loopback=True, on_line=self.on_line)
        self._current_runner = runner
        try:
            entry.case_result = runner.run()
        finally:
            self._current_runner = None
        if self._stop:
            entry.error = entry.error or "stopped by user"
        return entry

    # ------------------------------------------------------------------

    def _connect_with_retry(
        self, host: str, port: int, *, attempts: int = 3
    ) -> FramedSocket:
        """Connect with exponential backoff, abortable by ``self._stop``.

        The previous version slept through the full backoff window, so
        pressing Stop during a Server-unreachable retry made the UI
        sit at "Stopping..." until the third attempt finished. Now the
        sleep is polled in 100 ms ticks and bails immediately when the
        worker is asked to stop. (Task P, 2026-05-12.)
        """
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self._stop:
                raise ProtocolError(
                    f"stopped by user before connecting to {host}:{port}"
                )
            try:
                sock = socket.create_connection((host, port), timeout=5.0)
                sock.settimeout(None)
                return FramedSocket(sock)
            except OSError as exc:
                last_exc = exc
                if attempt < attempts:
                    deadline = time.monotonic() + backoff
                    while time.monotonic() < deadline:
                        if self._stop:
                            raise ProtocolError(
                                f"stopped by user while retrying connection to {host}:{port}"
                            ) from exc
                        time.sleep(0.1)
                    backoff *= 2
        raise ProtocolError(
            f"could not reach Server at {host}:{port} after {attempts} attempts: {last_exc}"
        )

    def _run_one_case(self, case: TestCase) -> SweepCaseEntry:
        assert self._sock is not None  # noqa: S101
        entry = SweepCaseEntry(case=case)
        self._emit("case_starting", {"case": case.label, "case_idx": case.index})

        try:
            self._sock.write_message(
                Message.start_case(
                    case_idx=case.index,
                    payload_bytes=case.payload_bytes,
                    bandwidth_mbps=case.bandwidth_mbps,
                    duration_s=case.duration_s,
                    protocol=self.cfg.test_plan.protocol,
                    server_ip=str(self.cfg.network.server_ip),
                    client_ip=str(self.cfg.network.client_ip),
                )
            )
            ready = self._sock.read_message(timeout_s=15.0)
            # Guard the payload access: a malformed SERVER_READY (missing /
            # non-numeric case_idx) must not raise a KeyError/ValueError that
            # escapes the `except (OSError, ProtocolError)` below and crashes
            # the sweep worker. Treat it as a non-ready reply instead.
            ready_idx = ready.payload.get("case_idx")
            try:
                ready_idx = int(ready_idx) if ready_idx is not None else None
            except (TypeError, ValueError):
                ready_idx = None
            if ready.type != "SERVER_READY" or ready_idx != case.index:
                entry.error = f"expected SERVER_READY for case {case.index}, got {ready.type}"
                return entry
        except (OSError, ProtocolError) as exc:
            # Round-6 (Task W): treat any I/O failure here as a lost
            # control channel. Round-8 (Task FF): unless the user just
            # pressed Stop, in which case the closed socket is expected.
            if self._stop:
                entry.error = "stopped by user"
            else:
                entry.error = str(exc)
                self._socket_alive = False
            return entry

        # Brief pause so the server's iperf3 -s socket is fully bound.
        time.sleep(0.25)

        runner = CaseRunner(self.cfg, case, loopback=False, on_line=self.on_line)
        self._current_runner = runner

        # Round-7 (Task DD, 2026-05-13): start a background thread that
        # watches the control socket while iperf3/fping are running. UDP
        # iperf3 doesn't notice a dead peer (it just keeps sending), so
        # without this poller the Client would only detect a mid-test
        # Server kill ~20-25 s later when CASE_DONE write finally fails.
        # The monitor uses select() + MSG_PEEK so it never consumes any
        # bytes the main thread is going to read; on EOF/RST it calls
        # runner.stop() to abort iperf3 immediately and flips
        # _socket_alive=False so the post-iperf3 code knows to bail.
        #
        # The monitor also watches the *physical link* of the NIC carrying
        # the control connection (resolved here, while the link is still
        # up). A pulled Ethernet cable doesn't send TCP FIN/RST — the
        # socket just goes silent — so select() alone would only notice at
        # the end-of-case control exchange (up to ~90 s later). Polling the
        # adapter's carrier catches an unplug within ~1 s.
        self._link_down = False
        link_adapter = self._resolve_link_adapter()
        monitor_stop = threading.Event()
        monitor_thread = threading.Thread(
            target=self._monitor_control_socket,
            args=(monitor_stop, link_adapter),
            daemon=True,
            name="ping-tool-socket-monitor",
        )
        monitor_thread.start()

        try:
            entry.case_result = runner.run()
        finally:
            monitor_stop.set()
            monitor_thread.join(timeout=2.0)
            self._current_runner = None

        # If the monitor caught a mid-case disconnect, surface it as the
        # entry error and bail without trying CASE_DONE (which would
        # raise OSError anyway).
        if self._stop:
            # Round-8 (Task FF, 2026-05-13): user-initiated stop on the
            # Client. The closed control socket isn't a server disconnect,
            # don't surface it as one.
            entry.error = entry.error or "stopped by user"
            return entry
        if not self._socket_alive:
            if self._link_down:
                entry.error = entry.error or (
                    "Network link down — Ethernet cable unplugged or the "
                    "wired adapter was disabled mid-case."
                )
            else:
                entry.error = (
                    entry.error
                    or "Server disconnected during case (monitor)"
                )
            return entry

        try:
            self._sock.write_message(Message.case_done(case_idx=case.index))
            srv = self._sock.read_message(
                timeout_s=case.duration_s * 2 + 30.0
            )
            if srv.type == "SERVER_RESULT":
                entry.server_iperf3_json = srv.payload.get("iperf3_json", "")
                entry.server_returncode = int(srv.payload.get("returncode", -1))
            else:
                entry.error = f"expected SERVER_RESULT, got {srv.type}"
        except (OSError, ProtocolError) as exc:
            # Round-6 (Task W): the most common failure mode is the
            # Server being killed mid-test. Round-8 (Task FF): a Stop
            # on the Client also closes this socket, mark accordingly.
            if self._stop:
                entry.error = "stopped by user"
            else:
                entry.error = str(exc)
                self._socket_alive = False

        return entry

    def _monitor_control_socket(
        self, stop_event: "threading.Event", link_adapter: str | None = None
    ) -> None:
        """Watch the control socket for peer EOF/RST while iperf3 runs.

        Runs on a daemon thread spawned by :meth:`_run_one_case`. Uses
        ``select.select`` with a 0.5 s tick so it wakes up promptly when
        ``stop_event`` is set, and ``socket.MSG_PEEK`` to inspect any
        pending bytes without consuming them (the main thread will read
        them after iperf3 completes).

        On EOF / RST / OSError it flips ``self._socket_alive`` to False
        and calls ``self._current_runner.stop()`` so iperf3 winds down
        immediately — without this the client would keep blasting UDP
        packets to a dead peer for the full case duration.

        ``link_adapter`` (when not None) is the NIC carrying this
        connection. Each tick we also poll its carrier: a pulled cable
        sends no FIN/RST, so the select() path never fires for it — the
        carrier poll is what catches an unplug in ~1 s. On link loss we
        set ``self._link_down`` (so the caller can word the error as a
        cable-pull) plus the same ``_socket_alive``/abort teardown.
        """
        sock = self._sock.sock if self._sock is not None else None
        if sock is None:
            return
        # Lazy import (psutil under the hood) — keeps core.control.client
        # import-light and matches the lazy-psutil pattern used elsewhere.
        from ..fix_actions import adapter_link_up

        while not stop_event.is_set():
            # Round-8 (Task FF, 2026-05-13): if the user pressed Stop,
            # exit promptly without flagging a server disconnect — the
            # main thread's stop() call is what closed the socket.
            if self._stop:
                return
            # Physical-link watch: a cable-pull / adapter-disable drops the
            # carrier (isup=False) while the TCP socket stays silently
            # ESTABLISHED, so this is the only signal that catches it fast.
            if link_adapter is not None and not adapter_link_up(link_adapter):
                return self._on_monitored_drop(stop_event, link_down=True)
            try:
                ready, _, errored = select.select([sock], [], [sock], 0.5)
            except (OSError, ValueError):
                # Socket was closed under us — treat as disconnect.
                return self._on_monitored_drop(stop_event)
            if errored:
                return self._on_monitored_drop(stop_event)
            if not ready:
                continue
            # Socket is readable. Could be (a) actual data the main
            # thread will consume after iperf3, (b) peer EOF, or (c) a
            # socket error. Distinguish via MSG_PEEK.
            try:
                peek = sock.recv(4096, socket.MSG_PEEK)
            except OSError:
                return self._on_monitored_drop(stop_event)
            if not peek:
                # Empty peek = peer closed connection cleanly.
                return self._on_monitored_drop(stop_event)
            # Real data sitting in the buffer — connection is fine, the
            # main thread will pick it up after iperf3 completes. Don't
            # consume it here, just keep polling.

    def _resolve_link_adapter(self) -> str | None:
        """NIC carrying the control connection — for the mid-case link watch.

        Thin wrapper over the shared :func:`core.fix_actions.adapter_for_socket`
        (which also serves the Server side) — resolves the control socket's
        local IPv4 to an adapter name so the monitor can poll its carrier.
        Returns ``None`` (→ monitor skips the link watch) for loopback / an
        unresolvable socket. Best-effort; never raises.
        """
        from ..fix_actions import adapter_for_socket

        sock = self._sock.sock if self._sock is not None else None
        return adapter_for_socket(sock)

    def _on_monitored_drop(
        self, stop_event: "threading.Event", *, link_down: bool = False
    ) -> None:
        """Handle a disconnect condition seen by :meth:`_monitor_control_socket`.

        Stays quiet (mutates nothing) when this monitor has already been told
        to stop — either the user pressed Stop (``self._stop``) or this case's
        ``stop_event`` is set. The latter guards the rare race where a monitor
        whose ``join(timeout=…)`` timed out wakes from a blocking ``select`` /
        psutil call *after* its case ended: without this check it could flip
        ``_socket_alive`` and trigger a spurious "disconnect" on the NEXT,
        healthy case. Otherwise it flags the control socket dead and tears down
        the in-flight iperf3 runner so it stops blasting UDP at a gone peer.
        ``link_down`` records a physical carrier loss so the caller words the
        error as a cable-pull. The monitor returns immediately after this.
        """
        if self._stop or stop_event.is_set():
            return
        if link_down:
            self._link_down = True
        self._socket_alive = False
        self._abort_current_runner()

    def _abort_current_runner(self) -> None:
        """Tear down the in-flight CaseRunner. Safe across threads."""
        runner = self._current_runner
        if runner is not None:
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, event: ClientEvent, data: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, data)
            except Exception:  # noqa: BLE001
                pass
