"""Run tab — role-aware test runner.

Three modes, one tab:

* **Loopback** (single laptop, dev mode): single-case runner with live log,
  charts, and metric panel.  Phase 2 functionality preserved verbatim.
* **Server** (Laptop A): auto-starts a ControlServer on a worker thread and
  shows the listening status + per-case event log.  No manual sweep button —
  the Server only obeys what the Client tells it.
* **Client** (Laptop B): "Run full sweep" button that drives the canonical
  20-case Test Procedure grid against the remote Server, with a 20-row
  table that fills in row-by-row plus a live log + charts for the active
  case.

Which panel is rendered depends on ``ctx.run_state.role`` at app launch.
Switching roles requires restarting the app (kept simple on purpose).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QDoubleValidator, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from ..context import AppContext, Role
from ..core.control.client import (
    MultiSweepResult,
    SweepResult,
    SweepSegment,
)
from ..core.parsers.iperf3 import parse_intervals
from ..core.prereq import wifi_adapters_on_test_subnet
from ..core.runner import estimate_case_wall_s, sweep_time_left_s
from ._sounds import SoundEvent
from ._sounds import notify_sound as _notify_sound
from ..reporting.run_report import fmt_duration
from ._base import BaseView, widen_detailed_box
from ._help_link import show_error_with_help
from ._validators import attach_filename_safe
from ._qt_runner import ServerWorker, SweepWorker, run_save_in_background
from ._sweep_table import SweepTable
from .save_report_dialog import (
    SaveDialogDecision,
    SaveDialogResult,
    SaveReportDialog,
)
from .segment_dialog import (
    BetweenSegmentsDialog,
    SegmentDecision,
    segment_display_name,
)


# fping data line.  Cygwin fping (4.2 and 5.5) with -D actually emits the
# timestamp on a *separate* line from the data, so we don't anchor on it; we
# just pull out the RTT.  Example data line:
#   192.168.1.1 : [18061], 84 bytes, 1.00 ms (0.97 avg, 0% loss)
_FPING_LIVE_RE = re.compile(
    r"\S+\s*:\s*\[\d+\],\s*\d+\s+bytes,\s+(?P<rtt>[\d.]+)\s+ms"
)


class ScriptView(BaseView):
    """Outer dispatcher: picks the right inner panel based on role."""

    title = "Test runner"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)

    def _build_placeholder(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        role = self.ctx.run_state.role
        if role is Role.SERVER:
            panel: QWidget = _ServerPanel(self.ctx)
        elif role is Role.CLIENT:
            panel = _ClientPanel(self.ctx)
        else:
            # Loopback (or undecided fallback) → the sweep panel pointed
            # at 127.0.0.1 (PP, Round 18 — replaces the old single-case
            # _LoopbackPanel; a one-row subset covers single-case checks).
            panel = _ClientPanel(self.ctx, loopback=True)
        # Kept as an attribute (not just a Qt child) so shutdown() can
        # reach the panel's background worker before this view is
        # destroyed — see shutdown().
        self._panel = panel
        outer.addWidget(panel)

    def shutdown(self) -> None:
        """Stop the inner panel's background worker before teardown.

        Invoked by :meth:`MainWindow.rebuild_script_tab` (role/config
        change) and :meth:`MainWindow.closeEvent` (app close) so the
        Server listener / sweep thread never outlives this view or the
        ``QApplication``.
        """
        panel = getattr(self, "_panel", None)
        if panel is not None and hasattr(panel, "shutdown"):
            try:
                panel.shutdown()
            except Exception:  # noqa: BLE001
                self.ctx.logger.exception("ScriptView panel shutdown failed")


# =============================================================================
# Server panel  (auto-listens, shows event log)
# =============================================================================


class _ServerPanel(QWidget):
    """Runs a ControlServer in the background and shows its event log."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._worker: ServerWorker | None = None
        self._cases_received = 0
        # Total cases for the *current* sweep, learned from the
        # START_SWEEP message the Client sends after HELLO_OK. None
        # means "no sweep in progress" or "legacy client that didn't
        # announce". We display "?" in the title in that case so the
        # user never sees a stale "/20" hardcoded denominator.
        self._sweep_total: int | None = None

        # --- whole-sweep ETA readout (mirrors the Client panel) ---
        # The Server never drives the schedule, but it receives the same
        # case_starting / case_done events the Client does, so it can time
        # each case locally and show the identical "elapsed · ~left"
        # countdown under "Cases received". All reset per sweep.
        self._case_t0: float | None = None  # monotonic at current case_starting
        self._sweep_t0: float | None = None  # monotonic at the sweep's first case
        self._completed_case_walls: list[float] = []  # measured per-case walls
        self._active_position: int | None = None  # in-flight case's 1-based slot
        self._active_total: int | None = None  # cases in the running sweep
        # 250 ms cadence matches the Client's progress timer so the
        # countdown ticks at the same rate. Armed at case_starting,
        # stopped at case_done / sweep_finished / disconnect.
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(250)
        self._eta_timer.timeout.connect(self._on_eta_tick)

        self._build()
        self._start_server()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        self._title_label = QLabel("<h2>Run — Server (waiting for Client)</h2>")
        outer.addWidget(self._title_label)
        _server_intro = QLabel(
            "This laptop just listens and obeys the Client's per-case "
            "start/stop — leave this window open during a sweep."
        )
        _server_intro.setWordWrap(True)
        outer.addWidget(_server_intro)

        status_box = QGroupBox("Server status")
        form = QFormLayout(status_box)
        self._status_label = QLabel("Starting listener…")
        self._status_label.setStyleSheet("font-weight:bold;")
        self._peer_label = QLabel("(no client yet)")
        self._case_label = QLabel("(idle)")
        self._count_label = QLabel("0")
        # Whole-sweep time readout, mirroring the Client panel's line under
        # its progress bar. Monospace + bold so the figures line up and read.
        self._remaining_label = QLabel("(idle)")
        self._remaining_label.setStyleSheet(
            "font-weight:bold; font-family:Consolas,'Courier New',monospace;"
        )
        form.addRow("Listener:", self._status_label)
        form.addRow("Connected client:", self._peer_label)
        form.addRow("Current case:", self._case_label)
        form.addRow("Cases received:", self._count_label)
        form.addRow("Sweep time:", self._remaining_label)
        outer.addWidget(status_box)

        # Event log.
        log_box = QGroupBox("Event log")
        log_layout = QVBoxLayout(log_box)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._log.setFont(mono)
        self._log.setMaximumBlockCount(5000)
        log_layout.addWidget(self._log)
        outer.addWidget(log_box, stretch=1)

        # Start / Stop / Restart row (Round-6, Task Z, 2026-05-13).
        # The previous version only had Stop server + Restart, and none
        # of the three actions wrote anything to the event log, so the
        # user couldn't tell whether their click had registered. The new
        # row adds:
        #   * Start server — bring the listener back up after a Stop.
        #   * Stop server  — same behaviour, now with a log line.
        #   * Restart server — renamed from "Restart"; logs both phases.
        # All three buttons enable/disable themselves based on whether
        # the worker is currently running, so the user can't double-
        # start or stop-when-already-stopped.
        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        self._start_btn = QPushButton("Start server")
        self._start_btn.setToolTip(
            "Start the control-channel listener so the Client can connect."
        )
        self._start_btn.clicked.connect(self._on_start_clicked)
        bottom_row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("Stop server")
        self._stop_btn.setToolTip(
            "Stop the listener. Any active client connection is closed."
        )
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        bottom_row.addWidget(self._stop_btn)
        self._restart_btn = QPushButton("Restart server")
        self._restart_btn.setToolTip(
            "Stop the listener and start a fresh one — useful after an IP/role change."
        )
        self._restart_btn.clicked.connect(self._on_restart_clicked)
        bottom_row.addWidget(self._restart_btn)
        outer.addLayout(bottom_row)
        # Initial state: server auto-starts in __init__, so Start is
        # disabled until the user stops it.
        self._refresh_button_states(running=True)

    # ----- lifecycle ---------------------------------------------------

    def _refresh_button_states(self, *, running: bool) -> None:
        """Enable / disable Start / Stop / Restart based on worker state."""
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._restart_btn.setEnabled(running)

    def _on_start_clicked(self) -> None:
        """User clicked Start server."""
        self._append_log("user clicked Start server")
        self._start_server()

    def _on_stop_clicked(self) -> None:
        """User clicked Stop server."""
        self._append_log("user clicked Stop server — telling listener to stop")
        self._stop_server()

    def _on_restart_clicked(self) -> None:
        """User clicked Restart server."""
        self._append_log("user clicked Restart server — stop + start")
        self._restart_server()

    def _start_server(self) -> None:
        # Round-7 (Task AA): wrap isRunning() in try/except because the
        # underlying C++ ServerWorker may have been deleted by deleteLater
        # already even though our Python reference is still around.
        if self._worker is not None:
            try:
                if self._worker.isRunning():
                    self._append_log("ignored Start — listener is already running")
                    return
            except RuntimeError:
                # libshiboken: "Internal C++ object already deleted" —
                # treat as "no live worker".
                self._worker = None
        worker = ServerWorker(self.ctx.config)
        worker.event.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_worker_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()
        self._append_log(
            f"starting listener on {self.ctx.config.network.server_ip}:"
            f"{self.ctx.config.network.control_port}"
        )
        self._status_label.setText("Starting listener…")
        self._refresh_button_states(running=True)
        self._warn_if_wifi_on_test_subnet()

    def _warn_if_wifi_on_test_subnet(self) -> None:
        """Log a WARNING when a Wi-Fi NIC shares the test subnet.

        The Server only listens (no sweep button to hard-block, unlike the
        Client), so the Server-side equivalent of the Client's _on_run block
        is a prominent event-log warning: a Wi-Fi adapter on the test subnet
        gives Windows a competing route to the iperf3 ``-s`` bind IP, which
        can corrupt the measured throughput/loss. Best-effort; never raises.
        """
        try:
            conflicts = wifi_adapters_on_test_subnet(self.ctx.config)
        except Exception:  # noqa: BLE001 - a detection hiccup must not break start
            return
        if not conflicts:
            return
        summary = ", ".join(f"{n} ({ip})" for n, ip in conflicts)
        self._append_log(
            f"WARNING: Wi-Fi on the test subnet — {summary}. It can steal the "
            "Server's test traffic; disable Wi-Fi (Setup tab) for accurate results."
        )

    def _stop_server(self) -> None:
        if self._worker is None or not self._worker.isRunning():
            self._append_log("ignored Stop — listener is not running")
            return
        self._worker.request_stop()
        self._status_label.setText("Stopping…")
        # The Stop and Restart buttons disable themselves so the user
        # can't click again while the stop is in flight. The Start
        # button stays disabled until ``_on_worker_finished`` fires.
        self._stop_btn.setEnabled(False)
        self._restart_btn.setEnabled(False)

    def _restart_server(self) -> None:
        """Stop, wait for the worker to exit, then start a fresh one."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._status_label.setText("Restarting — stopping current listener…")
            self._refresh_button_states(running=False)
            # Round-6 (Task Z): bumped from 2000 ms to 5000 ms. With the
            # ControlServer fix from Task X the worker should exit within
            # ~1 s on a Stop click, but on a busy Windows host the
            # accept-loop's 0.5 s timeout + the read-loop's 1 s tick can
            # push that out a bit. 5 s is generous without being annoying.
            if not self._worker.wait(5000):
                self._append_log(
                    "WARNING: listener didn't stop within 5 s — starting a "
                    "fresh one anyway; the old thread will exit on its own."
                )
            else:
                self._append_log("listener stopped — bringing up a fresh one")
        self._worker = None
        self._cases_received = 0
        self._sweep_total = None
        self._count_label.setText("0")
        self._peer_label.setText("(no client yet)")
        self._case_label.setText("(idle)")
        self._reset_eta_state()
        self._start_server()

    @Slot()
    def _on_worker_finished(self) -> None:
        """Server thread exited — refresh status + button states.

        Round-7 (Task AA + BB, 2026-05-13): when the user clicks Restart
        server, the old worker's finished signal can arrive AFTER we've
        already started a new worker (because QThread.wait() blocks the
        main event loop, so the queued finished signal only delivers
        after _start_server returned). Without a sender() check the stale
        signal would disable Stop/Restart on the brand-new worker.

        Also clears ``self._worker`` so a subsequent Start-server click
        doesn't crash with ``libshiboken: Internal C++ object
        (ServerWorker) already deleted`` when checking ``isRunning()`` on
        a shadow reference whose underlying Qt object was already
        destroyed by ``deleteLater``.
        """
        finished_worker = self.sender()
        if finished_worker is not self._worker:
            # Stale signal from a worker we've already replaced. Don't
            # touch UI state — the current worker is happily running.
            return
        self._worker = None
        self._append_log("listener stopped")
        text = self._status_label.text().lower()
        if text.startswith(("stopping", "restarting", "starting")) or text == "":
            self._status_label.setText("Stopped")
        self._refresh_button_states(running=False)

    def shutdown(self) -> None:
        """Stop the listener thread and block until it exits.

        Called by :class:`ScriptView` on teardown — a role/config change
        rebuilds the Run tab, and the app close closes the window.
        The ``ServerWorker`` is not a Qt child of this panel, so without
        an explicit stop+wait the listener thread would outlive the panel
        (and, on app close, the ``QApplication``) — Qt then logs
        ``QThread: Destroyed while thread is still running`` and the
        bundled iperf3 server subprocess can be orphaned.
        """
        try:
            self._eta_timer.stop()
        except RuntimeError:
            pass
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.request_stop()
                worker.wait(5000)
        except RuntimeError:
            # libshiboken: underlying C++ object already deleted — nothing
            # left to stop.
            pass

    # ----- events -------------------------------------------------------

    @Slot(str, dict)
    def _on_event(self, name: str, data: dict) -> None:
        self._append_log(f"{name}: {data}")
        if name == "waiting_for_bind":
            # The configured IP isn't bindable yet (DHCP/APIPA boot, or a
            # just-set static IP still going live). The listener keeps
            # retrying and comes up automatically — no restart needed.
            host = data.get("host")
            self._status_label.setText(
                f"Waiting for {host} to come up on a NIC — "
                "the listener will start automatically (no restart needed)."
            )
            self._set_title("waiting for IP")
        elif name == "listening":
            self._status_label.setText(
                f"Listening on {data.get('host')}:{data.get('port')}"
            )
            self._set_title("waiting for Client")
            # Listener is healthy again — drop any stale connection banner.
            self._set_connection_warning("")
        elif name == "client_connected":
            self._peer_label.setText(str(data.get("peer", "(unknown)")))
            self._set_title(f"Client connected: {data.get('peer', '?')}")
            # Refresh the Listener line so a leftover "Sweep finished" from
            # the previous run doesn't linger once a new client connects.
            self._status_label.setText("Client connected — ready")
            # A fresh client means the previous drop is resolved — clear it.
            self._set_connection_warning("")
        elif name == "client_disconnected":
            self._peer_label.setText("(disconnected)")
            self._case_label.setText("(idle)")
            # Client is gone — stop the countdown and clear the readout.
            self._reset_eta_state()
            # Don't reset to "waiting" if a sweep just finished — keep
            # that status visible until next client connects.
            # Defensive: if the client drops mid-sweep, release the
            # role-switch lock so the user isn't stuck.
            self.ctx.run_state.sweep_active = False
        elif name == "sweep_starting":
            # New protocol: Client just announced a fresh sweep with
            # ``total_cases`` cases. Reset our per-sweep counters so the
            # title denominator is right and "Cases received: M" doesn't
            # carry numbers over from a previous sweep on the same
            # listener.
            self._cases_received = 0
            total = int(data.get("total_cases", 0))
            self._sweep_total = total if total > 0 else None
            self._count_label.setText("0")
            # Arm the ETA readout for the new sweep — it begins counting
            # at the first case_starting.
            self._reset_eta_state(idle_text="waiting for first case…")
            sweep_id = data.get("sweep_id", "")
            id_suffix = f" (id={sweep_id})" if sweep_id else ""
            self._set_title(
                f"sweep starting — {self._fmt_total()} cases{id_suffix}"
            )
            # Update the Listener line too — without this it kept showing the
            # PREVIOUS run's "Sweep finished" while a new sweep was running.
            self._status_label.setText(
                f"Sweep in progress — {self._fmt_total()} cases"
            )
            # New sweep underway — clear any banner left by a prior drop.
            self._set_connection_warning("")
            self.ctx.run_state.sweep_active = True
        elif name == "case_starting":
            self._case_label.setText(str(data.get("case", "?")))
            # Prefer position/total reported by the Server (post-START_SWEEP),
            # fall back to local counters for older clients.
            position = int(data.get("position", self._cases_received + 1))
            self._set_title(
                f"running {data.get('case', '?')} "
                f"({position}/{self._fmt_total()})"
            )
            # Mirror the Client's whole-sweep ETA: time each case locally.
            # Arm the sweep clock on the first case; later cases leave it.
            self._case_t0 = time.monotonic()
            if self._sweep_t0 is None:
                self._sweep_t0 = time.monotonic()
            self._active_position = position
            total = data.get("total_cases", self._sweep_total)
            self._active_total = int(total) if total else None
            if not self._eta_timer.isActive():
                self._eta_timer.start()
            # Paint the readout now rather than waiting up to 250 ms.
            self._on_eta_tick()
            # Server is mid-sweep: block role switching.
            self.ctx.run_state.sweep_active = True
        elif name == "case_done":
            self._cases_received += 1
            self._count_label.setText(str(self._cases_received))
            self._case_label.setText(
                f"finished {data.get('case', '?')} (rc={data.get('returncode', '?')})"
            )
            # Record this case's real wall time so the remaining estimate
            # is measured, not guessed, after case 1 (mirrors the Client).
            # Freeze the readout during the brief inter-case gap by stopping
            # the timer; the next case_starting re-arms it.
            if self._case_t0 is not None:
                self._completed_case_walls.append(
                    time.monotonic() - self._case_t0
                )
            self._case_t0 = None
            self._eta_timer.stop()
        elif name == "sweep_finished":
            self._status_label.setText("Sweep finished")
            self._case_label.setText("(idle)")
            cases = int(data.get("cases", 0))
            self._set_title(
                f"sweep finished — {cases}/{self._fmt_total()} cases"
            )
            # Sweep complete — clear the total so the next sweep starts fresh.
            self._sweep_total = None
            self._reset_eta_state()
            self.ctx.run_state.sweep_active = False
        elif name == "error":
            # Freeze the countdown; a following client_disconnected /
            # sweep_finished resets it. (Transient errors mid-sweep are
            # rare; the next case_starting re-arms the timer.)
            self._eta_timer.stop()
            msg = str(data.get("message", "unknown"))
            self._status_label.setText(f"ERROR: {msg}")
            # Raise the same cross-tab orange banner the Client shows on a
            # connection error, so a mid-sweep drop (client gone, cable
            # pulled, control-channel timeout) is visible on every tab —
            # not buried in the Server status box. It clears on the next
            # client_connected / sweep_starting / listening. The persistent
            # banner does NOT fire on the benign client_disconnected that
            # follows a normal sweep, so a clean run leaves it untouched.
            self._set_connection_warning(f"Client connection error: {msg}")
            # Header stays calm ("not listening") so the word "error" doesn't
            # alarm the user in the tab heading — the technical reason (e.g. a
            # bind failure) is shown in the Server status box above.
            self._set_title("not listening")

    def _set_connection_warning(self, text: str) -> None:
        """Set (or clear, with "") the cross-tab orange warning banner.

        Mirrors the Client panel's connection-error path: writes
        ``ctx.run_state.connection_warning_text`` and asks the main window
        to re-render its persistent banner so the warning shows on every
        tab. Safe to call before the panel is parented into the window —
        the ``hasattr`` guard makes it a no-op then.
        """
        self.ctx.run_state.connection_warning_text = text
        window = self.window()
        if hasattr(window, "refresh_warning_banner"):
            window.refresh_warning_banner()

    # ----- whole-sweep ETA readout (mirrors _ClientPanel) --------------

    def _reset_eta_state(self, *, idle_text: str = "(idle)") -> None:
        """Stop the countdown timer and clear per-sweep timing state."""
        self._eta_timer.stop()
        self._case_t0 = None
        self._sweep_t0 = None
        self._completed_case_walls = []
        self._active_position = None
        self._active_total = None
        self._remaining_label.setText(idle_text)

    def _per_case_estimate_s(self) -> float:
        """Best estimate of one case's wall-clock time, in seconds.

        Mirrors :meth:`_ClientPanel._per_case_estimate_s`: the measured
        average of cases already finished this sweep, falling back to the
        static duration-aware model before the first case completes.
        """
        if self._completed_case_walls:
            return sum(self._completed_case_walls) / len(self._completed_case_walls)
        return estimate_case_wall_s(
            float(self.ctx.config.test_plan.duration_s),
            float(self.ctx.config.fping.interval_ms),
        )

    @Slot()
    def _on_eta_tick(self) -> None:
        """Refresh the 'Sweep time' readout while a case is in flight.

        Mirrors :meth:`_ClientPanel._on_progress_tick`'s whole-sweep ETA:
        elapsed since the sweep's first case, plus an estimate of the time
        left across every case still queued (the in-flight case included).
        """
        if self._case_t0 is None or self._sweep_t0 is None:
            return
        now = time.monotonic()
        sweep_elapsed = now - self._sweep_t0
        total = self._active_total
        position = self._active_position
        if not total or position is None:
            # A legacy / minimal Client sent no total_cases, so we can't
            # estimate the time *left* — but elapsed is still meaningful.
            # Show elapsed-only rather than freezing the readout while the
            # operator can plainly see cases running.
            self._remaining_label.setText(f"{fmt_duration(sweep_elapsed)} elapsed")
            return
        case_total_s = max(1.0, self._per_case_estimate_s())
        sweep_left = sweep_time_left_s(
            position=position,
            total=total,
            case_total_s=case_total_s,
            case_elapsed_s=now - self._case_t0,
        )
        self._remaining_label.setText(
            f"{fmt_duration(sweep_elapsed)} elapsed  ·  "
            f"~{fmt_duration(sweep_left)} left"
        )

    def _fmt_total(self) -> str:
        """Render the current sweep's total as a string for title text.

        ``"?"`` means we haven't seen a START_SWEEP yet (or it carried 0).
        That's better than the old hardcoded ``"20"`` — the user can
        immediately tell the Server is talking to a legacy Client or
        that the sweep hasn't formally started.
        """
        return str(self._sweep_total) if self._sweep_total else "?"

    def _set_title(self, suffix: str) -> None:
        self._title_label.setText(f"<h2>Run — Server ({suffix})</h2>")

    def _append_log(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._log.appendPlainText(f"[{ts}] {line}")


# =============================================================================
# Client panel  (Run full sweep + 20-row table + per-case live log)
# =============================================================================


class _ClientPanel(QWidget):
    """Drives a full sweep — against a remote Server (Client role) or
    entirely on 127.0.0.1 (``loopback=True``, Loopback role)."""

    def __init__(self, ctx: AppContext, *, loopback: bool = False) -> None:
        super().__init__()
        self.ctx = ctx
        self._loopback = loopback
        self._worker: SweepWorker | None = None
        self._t0: float | None = None
        self._tp_data: list[tuple[float, float]] = []
        self._lat_data: list[tuple[float, float]] = []
        # Subset for the *currently-running* sweep. Empty list = no
        # active sweep or full 20-case run; populated in :meth:`_on_run`.
        self._active_subset: list[int] = []

        # ----- Group C-1: multi-segment state -----
        # Populated in continuous mode; left empty otherwise. The
        # orchestration state machine is driven entirely from
        # _on_sweep_finished and the between-segments dialog response.
        self._multi_in_progress: bool = False
        self._multi_started_at: float = 0.0
        self._multi_segments: list = []  # list[SweepSegment]
        # Label for the *currently-running* segment. Set when the
        # SweepWorker is kicked off (either from _on_run or from the
        # Continue path of the between-segments dialog).
        self._current_segment_label: str = ""
        # 1-based index of the currently-running segment.
        self._current_segment_idx: int = 0
        # Raw text the operator typed for segment 1 before the run started.
        # Captured at run start so the live "current segment label" field
        # (overwritten as segments advance) can be restored to the
        # operator's original entry once the multi-segment run ends.
        self._first_segment_label_text: str = ""

        # Captured error message during the most recent sweep, if any.
        # Set by ``_on_event(name='error', ...)`` and consumed by the
        # finished handler so a Server-bind failure or a HELLO timeout
        # shows up in the popup / status banner instead of silently
        # presenting as "0/0 ok". (Task F, 2026-05-12.)
        self._last_sweep_error: str = ""

        # Flagged True by _on_stop so the finished handler can
        # distinguish a user-aborted run from a connection error.
        # (Task M, 2026-05-12.)
        self._user_stopped: bool = False

        # Smooth-progress state. Without these the per-sweep bar only
        # nudges forward at case_done, so the user sees a frozen bar
        # during the 30s a case is running. ``_case_t0`` is the
        # monotonic wall-clock of the current case_starting; the
        # 250ms QTimer below uses it to interpolate sub-case progress.
        # All four fields are reset at sweep start and on case
        # boundaries.
        self._case_t0: float | None = None
        self._active_case_position: int | None = None
        self._active_case_label: str = ""
        self._active_case_duration_s: int = self.ctx.config.test_plan.duration_s
        # Monotonic wall-clock of the *first* case_starting in the
        # current sweep (or segment). Drives the whole-sweep ETA
        # readout under the progress bar; reset per segment in
        # _start_sweep_worker so continuous-mode ETAs track only the
        # segment in flight.
        self._sweep_t0: float | None = None
        # Wall-clock duration of each case finished in the current
        # sweep / segment. Their running average is the adaptive
        # per-case estimate (see _per_case_estimate_s) — it makes the
        # ETA self-correct after case 1, regardless of duration / fping
        # interval / machine speed. Reset per segment.
        self._completed_case_walls: list[float] = []

        # 250 ms cadence is fast enough that the bar looks animated
        # without spamming repaints. Started at case_starting, stopped
        # at case_done / sweep_finished / on stop.
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(250)
        self._progress_timer.timeout.connect(self._on_progress_tick)

        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        if self._loopback:
            outer.addWidget(QLabel(
                "<h2>Run — Loopback (drive a full sweep on 127.0.0.1)</h2>"
            ))
            _intro = QLabel(
                "Press <b>Run sweep</b> to run the whole grid on "
                "<b>127.0.0.1</b> (no second laptop) — Subset skips cases, "
                "Continuous mode runs sweeps back-to-back."
            )
        else:
            outer.addWidget(QLabel(
                "<h2>Run — Client (drive a full sweep)</h2>"
            ))
            host = (self.ctx.run_state.server_host_override
                    or str(self.ctx.config.network.server_ip))
            _intro = QLabel(
                f"Press <b>Run sweep</b> to run the whole grid against the "
                f"Server at <b>{host}</b> — Subset skips cases, Continuous mode "
                "runs sweeps back-to-back."
            )
        _intro.setWordWrap(True)
        outer.addWidget(_intro)

        # ----- top control row: just the action buttons (right-aligned) -----
        control_row = QHBoxLayout()
        control_row.addStretch(1)
        self._run_btn = QPushButton("Run sweep")
        self._run_btn.clicked.connect(self._on_run)
        control_row.addWidget(self._run_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        control_row.addWidget(self._stop_btn)
        outer.addLayout(control_row)

        # ----- status line ABOVE the bar (prominent, not greyed) -----
        # Shows "Ready." when idle and the running-case / phase text during a
        # sweep (e.g. "Case 1/1: #01 payload=200B bw=10M · 10s/48s"). This is
        # the single status line — the per-case text used to be hidden inside
        # the bar; now it's a bold line right above it.
        self._status_label = QLabel("Ready.")
        self._status_label.setStyleSheet("font-weight:bold;")
        self._status_label.setWordWrap(True)
        outer.addWidget(self._status_label)

        # ----- per-sweep progress bar (visual % only; text is the line above) -----
        self._progress = QProgressBar()
        self._progress.setRange(0, 20)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)  # default "%p%"
        outer.addWidget(self._progress)

        # ----- whole-sweep time readout, UNDER the bar (prominent, not greyed) -----
        # How long the sweep has been running plus a rolling estimate of time
        # left. Monospace so the figures line up; bold accent so it reads.
        self._sweep_eta_label = QLabel("")
        self._sweep_eta_label.setStyleSheet(
            "font-weight:bold; font-family:Consolas,'Courier New',monospace;"
        )
        outer.addWidget(self._sweep_eta_label)

        # ----- controls: Subset (left) + Continuous (right), kept separate -----
        # Split 50/50 with an 8 px gap so the divider falls at the middle of
        # the tab and lines up exactly with the table | charts splitter below
        # (same 8 px handle), giving both rows an identical, aligned gap.
        _SPLIT_GAP = 8
        controls_row = QHBoxLayout()
        controls_row.setSpacing(_SPLIT_GAP)
        controls_row.addWidget(self._build_subset_box(), stretch=1)
        # Right column stacks the Continuous box and the (separate) Cable
        # length box, so cable length reads as its own section under the
        # Continuous controls without disturbing the 50/50 split below.
        right_col = QVBoxLayout()
        right_col.setSpacing(_SPLIT_GAP)
        right_col.addWidget(self._build_continuous_box())
        right_col.addWidget(self._build_cable_length_box())
        right_col.addStretch(1)
        controls_row.addLayout(right_col, stretch=1)
        outer.addLayout(controls_row)

        # ----- main split: left = sweep table, right = live log + charts -----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(_SPLIT_GAP)

        self._sweep_table = SweepTable(self.ctx.config)
        self._sweep_table.selection_changed.connect(self._on_selection_changed)
        splitter.addWidget(self._sweep_table)

        right_tabs = QTabWidget()
        right_tabs.addTab(self._build_log_tab(), "Live log")
        right_tabs.addTab(self._build_chart_tab(), "Charts")
        splitter.addWidget(right_tabs)

        # 50/50 and stays 50/50 on resize: equal stretch + equal initial sizes
        # (Qt scales setSizes proportionally to the available width).
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1_000_000, 1_000_000])
        outer.addWidget(splitter, stretch=1)

        # Apply any persisted Group B subset before the user clicks Run.
        # set_selected_case_indexes() emits selection_changed so the
        # counter chip + Run-button label update without a manual call.
        persisted = list(self.ctx.run_state.selected_case_indexes or [])
        if persisted:
            self._sweep_table.set_selected_case_indexes(persisted)
        else:
            # Force one update so the chip starts at "20 of 20" instead
            # of empty - selection_changed only fires on actual changes.
            self._on_selection_changed(self._sweep_table.selected_count())

        # Apply persisted Group C-1 continuous-mode toggle. Setting the
        # check state fires the toggled signal so the label-input
        # visibility lines up automatically.
        self._continuous_check.setChecked(self.ctx.run_state.continuous_mode)
        # Make sure the dependent widgets reflect the initial state even
        # if the check was already at the right value (which wouldn't
        # fire toggled).
        self._on_continuous_toggled(self._continuous_check.isChecked())

    def _set_progress_text(self, text: str) -> None:
        """Set the status / running-case text shown on the line above the bar."""
        self._status_label.setText(text)

    def _build_continuous_box(self) -> QGroupBox:
        """Group C-1: Continuous (multi-segment) mode toggle + segment label.

        Kept separate from the Subset box (placed beside it in the Run
        layout). The label field is the *first* segment's name — segments
        2..N are prompted via the between-segments dialog at run time.
        """
        box = QGroupBox("Continuous (multi-segment) mode")
        layout = QGridLayout(box)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        # Short label (the right column is narrower); the full explanation
        # lives in the tooltip + the group-box title.
        self._continuous_check = QCheckBox("Run sweeps back-to-back")
        self._continuous_check.setToolTip(
            "When ticked, after each sweep finishes you'll get a dialog "
            "with options to Continue with the next segment, Retry this "
            "segment, or Save and finish. The whole run rolls up into "
            "one multi-segment report."
        )
        self._continuous_check.toggled.connect(self._on_continuous_toggled)
        layout.addWidget(self._continuous_check, 0, 0, 1, 2)

        # Segment-1 label input (segments 2..N come from the between-
        # segments dialog). Hidden when continuous mode is off so the
        # single-sweep UX stays as it was.
        self._segment_label_caption = QLabel("Current segment label:")
        self._segment_label_caption.setStyleSheet("color:#8a97ad;")
        layout.addWidget(self._segment_label_caption, 1, 0)

        self._segment_label_edit = QLineEdit()
        attach_filename_safe(
            self._segment_label_edit,
            "Segment label. Used in multi-segment report folder + xlsx sheet names.\n"
            "Forbidden: < > | \" * ? : / \\",
            allow_blank=True,
        )
        self._segment_label_edit.setPlaceholderText("Segment 1")
        self._segment_label_edit.setToolTip(
            "Free-text identifier for the segment currently running. Type "
            "the first segment's name here before you start; the field then "
            "tracks the active segment as you advance through the run. "
            "Defaults to 'Segment N' if left blank."
        )
        layout.addWidget(self._segment_label_edit, 1, 1)

        layout.setColumnStretch(1, 1)
        layout.setRowStretch(2, 1)  # keep the box top-aligned beside Subset
        return box

    def _build_cable_length_box(self) -> QGroupBox:
        """Optional 'cable length under test' input (metres) → reports.

        A separate section under the Continuous box. Numeric-only via
        QDoubleValidator (up to two decimals, e.g. ``12.50``); blank means
        "not recorded". The value is mirrored into RunState and captured into
        every saved report's metadata block (see reporting.metadata_rows).
        """
        box = QGroupBox("Cable length")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        layout.addWidget(QLabel("Cable length (m):"))

        self._cable_length_edit = QLineEdit()
        self._cable_length_edit.setPlaceholderText("e.g. 12.50  (optional)")
        validator = QDoubleValidator(0.0, 99999.99, 2, self._cable_length_edit)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self._cable_length_edit.setValidator(validator)
        self._cable_length_edit.setToolTip(
            "Optional length of the cable under test, in metres (e.g. 12.50).\n"
            "Numbers only, up to two decimals. Appears in the saved report."
        )
        self._cable_length_edit.setText(self.ctx.run_state.cable_length_m or "")
        self._cable_length_edit.textChanged.connect(self._on_cable_length_changed)
        layout.addWidget(self._cable_length_edit, stretch=1)
        return box

    @Slot(str)
    def _on_cable_length_changed(self, text: str) -> None:
        """Mirror the cable-length field into RunState (read at report time)."""
        self.ctx.run_state.cable_length_m = text.strip()

    def _build_subset_box(self) -> QGroupBox:
        """Group B: Select-all / Select-none + payload + bandwidth toggles."""
        box = QGroupBox("Sweep subset — uncheck rows in the table to skip them")
        layout = QGridLayout(box)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(4)

        # Row 0: counter chip + select-all/select-none.
        self._counter_chip = QLabel("20 of 20 selected")
        self._counter_chip.setStyleSheet(
            "padding:3px 8px; background:#1565c0; color:#fff;"
            "border-radius:8px; font-weight:bold;"
        )
        self._counter_chip.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self._counter_chip, 0, 0)

        select_all_btn = self._compact_btn("Select all")
        select_all_btn.clicked.connect(self._on_select_all)
        layout.addWidget(select_all_btn, 0, 1)
        select_none_btn = self._compact_btn("Select none")
        select_none_btn.clicked.connect(self._on_select_none)
        layout.addWidget(select_none_btn, 0, 2)

        layout.setColumnStretch(7, 1)  # eat trailing space

        # Row 1: payload toggles.
        layout.addWidget(self._dim_label("Payload:"), 1, 0)
        for col_offset, payload in enumerate(self.ctx.config.test_plan.payloads_bytes):
            btn = self._compact_btn(f"{payload} B")
            btn.setToolTip(
                f"Toggle every case with payload = {payload} B (5 cases)."
            )
            btn.clicked.connect(
                lambda _checked=False, p=payload: self._sweep_table.toggle_payload(p)
            )
            layout.addWidget(btn, 1, 1 + col_offset)

        # Row 2: bandwidth toggles.
        layout.addWidget(self._dim_label("Bandwidth:"), 2, 0)
        for col_offset, bw in enumerate(self.ctx.config.test_plan.bandwidths_mbps):
            btn = self._compact_btn(f"{bw} Mbps")
            btn.setToolTip(
                f"Toggle every case with bandwidth = {bw} Mbps (4 cases)."
            )
            btn.clicked.connect(
                lambda _checked=False, b=bw: self._sweep_table.toggle_bandwidth(b)
            )
            layout.addWidget(btn, 2, 1 + col_offset)

        layout.setRowStretch(3, 1)  # keep the rows top-packed; box stays short
        return box

    @Slot(bool)
    def _on_continuous_toggled(self, checked: bool) -> None:
        """Mirror the checkbox into RunState and show/hide the label input."""
        self.ctx.run_state.continuous_mode = checked
        # Label input is only meaningful in multi-segment mode — keep
        # the single-sweep UX uncluttered.
        self._segment_label_caption.setVisible(checked)
        self._segment_label_edit.setVisible(checked)

    @staticmethod
    def _dim_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#aaa;")
        return lbl

    @staticmethod
    def _compact_btn(text: str) -> QPushButton:
        """A Sweep-subset toggle button, shorter than the default.

        Overrides only the global button's min-height + padding (the border /
        accent hover styling is inherited from the app QSS), so the three
        button rows take less height and the table + charts below get more.
        """
        btn = QPushButton(text)
        btn.setStyleSheet("min-height: 14px; padding: 3px 12px;")
        return btn

    @Slot()
    def _on_select_all(self) -> None:
        self._sweep_table.select_all()

    @Slot()
    def _on_select_none(self) -> None:
        self._sweep_table.select_none()

    @Slot(int)
    def _on_selection_changed(self, selected: int) -> None:
        """Update counter chip + Run button label whenever selection changes."""
        total = self._sweep_table.total_count()
        eta = self._sweep_table.estimated_duration_s()
        eta_text = fmt_duration(eta) if selected > 0 else "0s"

        self._counter_chip.setText(
            f"{selected} of {total} selected · est. {eta_text}"
        )
        # Greyed-out chip when the user has unchecked everything; warning
        # colour when only a partial subset is selected; default blue
        # only at full 20/20.
        if selected == 0:
            colour = "#444"
        elif selected < total:
            colour = "#b77c11"
        else:
            colour = "#1565c0"
        self._counter_chip.setStyleSheet(
            f"padding:3px 8px; background:{colour}; color:#fff;"
            "border-radius:8px; font-weight:bold;"
        )

        # Mirror the new selection back into RunState so QSettings can
        # persist it on app close. Empty list = "all 20".
        rs = self.ctx.run_state
        rs.selected_case_indexes = (
            self._sweep_table.selected_case_indexes()
            if 0 < selected < total
            else []
        )

        # Run button label + enabled state.
        if selected == 0:
            self._run_btn.setText("Run sweep")
            self._run_btn.setEnabled(False)
        elif selected == total:
            self._run_btn.setText("Run full sweep")
            self._run_btn.setEnabled(not self._is_busy())
        else:
            self._run_btn.setText(f"Run subset ({selected} cases)")
            self._run_btn.setEnabled(not self._is_busy())

    def _is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _build_log_tab(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._log.setFont(mono)
        self._log.setMaximumBlockCount(20000)
        layout.addWidget(self._log)
        return wrap

    def _build_chart_tab(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        pg.setConfigOptions(antialias=True)

        self._tp_plot = pg.PlotWidget(title="Throughput (Mbps) — current case")
        self._tp_plot.setBackground("#1e1e1e")
        self._tp_plot.setLabel("left", "Mbps")
        self._tp_plot.setLabel("bottom", "elapsed (s)")
        self._tp_plot.showGrid(x=True, y=True, alpha=0.3)
        self._tp_curve = self._tp_plot.plot(pen=pg.mkPen("#42a5f5", width=2))

        self._lat_plot = pg.PlotWidget(title="Latency (ms) — current case")
        self._lat_plot.setBackground("#1e1e1e")
        self._lat_plot.setLabel("left", "ms")
        self._lat_plot.setLabel("bottom", "elapsed (s)")
        self._lat_plot.showGrid(x=True, y=True, alpha=0.3)
        self._lat_curve = self._lat_plot.plot(pen=pg.mkPen("#ef5350", width=2))

        layout.addWidget(self._tp_plot)
        layout.addWidget(self._lat_plot)
        return wrap

    # ----- run lifecycle ----------------------------------------------

    @Slot()
    def _on_run(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        # Snapshot the user's subset before locking the table.
        selected = self._sweep_table.selected_case_indexes()
        if not selected:
            # Defensive — Run button should already be disabled.
            self._status_label.setText("No cases selected.")
            return

        # ----- Hard block: Wi-Fi on the test subnet would corrupt the run -----
        # A Wi-Fi NIC holding an IPv4 on the *same* subnet as the test link
        # gives Windows a competing route to the server IP, so iperf3/fping
        # traffic can leave over Wi-Fi instead of the dedicated Ethernet
        # cable — silently wrong latency/throughput. Refuse to start until
        # it's off (Setup tab → "Disconnect Wi-Fi"; auto-restored on close
        # / Reset). Loopback runs on 127.0.0.1 so Wi-Fi can't affect it.
        if not self._loopback:
            wifi_conflicts = wifi_adapters_on_test_subnet(self.ctx.config)
            if wifi_conflicts:
                summary = "\n".join(f"  •  {n} — {ip}" for n, ip in wifi_conflicts)
                _notify_sound(SoundEvent.ERROR)
                QMessageBox.critical(
                    self,
                    "Wi-Fi on the test subnet — sweep blocked",
                    "A Wi-Fi adapter is connected on the same subnet as the "
                    "test link, so the measurement could travel over Wi-Fi "
                    "instead of the Ethernet cable and be wrong.\n\n"
                    f"Conflicting adapter(s):\n{summary}\n\n"
                    "Open the Setup tab and click “Disconnect Wi-Fi” "
                    "(PingPair re-enables it automatically when you close the "
                    "app or hit Reset), then start the sweep again.",
                )
                self._status_label.setText(
                    "Sweep blocked — Wi-Fi is on the test subnet "
                    "(disable it on the Setup tab)."
                )
                return

        # ----- Group C-1: open a multi-segment run if enabled -----
        # The subset is shared across all segments (locked in 2026-05-11
        # design call), so we capture it here once. Per-segment label
        # comes from the panel's First-segment-label field; segments
        # 2..N are prompted via the between-segments dialog.
        if self.ctx.run_state.continuous_mode:
            self._multi_in_progress = True
            self._multi_started_at = time.time()
            self._multi_segments = []
            self._current_segment_idx = 1
            self._first_segment_label_text = self._segment_label_edit.text()
            self._current_segment_label = (
                self._first_segment_label_text.strip() or "Segment 1"
            )
            # Turn the field into a live read-out of the segment now in
            # flight (it used to stay frozen on segment 1's text, which read
            # as a stuck value). Restored to the operator's entry when the
            # run ends — see _reset_after_zero_case_finish / finalize.
            self._segment_label_edit.setText(self._current_segment_label)
            # Lock the continuous-mode controls for the duration of the
            # multi-segment run so the operator can't flip the mode
            # mid-flight.
            self._continuous_check.setEnabled(False)
            self._segment_label_edit.setEnabled(False)
        else:
            self._multi_in_progress = False
            self._current_segment_idx = 0
            self._current_segment_label = ""

        self._start_sweep_worker(selected)

    def _start_sweep_worker(self, selected: list[int]) -> None:
        """Kick off a SweepWorker for one segment (or one single sweep).

        Shared by :meth:`_on_run` and the Continue / Retry paths from
        the between-segments dialog. ``selected`` is the active subset
        (already validated non-empty by the caller).
        """
        self._sweep_table.reset()
        # Re-apply the selection (reset() preserves it, but mark the
        # unchecked rows visually as Skipped so the user sees what's
        # going to be left out at a glance).
        self._sweep_table.set_selected_case_indexes(selected)
        self._sweep_table.mark_skipped_unselected()
        self._sweep_table.set_interactive(False)

        self._log.clear()
        self._reset_chart()
        # Progress bar tops out at the subset size × 100 — the *100
        # gives us sub-case granularity so the QTimer can advance the
        # bar smoothly within a single case (otherwise the user sees
        # a frozen bar for the 30 s a case is running).
        self._progress.setRange(0, len(selected) * 100)
        self._progress.setValue(0)

        # Status / progress text differs in multi-segment mode so the
        # operator always sees which segment is in flight.
        if self._multi_in_progress:
            # Loopback has no Server to connect to — say "Starting".
            verb = "Starting" if self._loopback else "Connecting"
            tail = "starting…" if self._loopback else "connecting to Server…"
            seg_name = segment_display_name(
                self._current_segment_idx, self._current_segment_label
            )
            self._set_progress_text(f"{seg_name} · {verb}…")
            self._status_label.setText(f"{seg_name}: {tail}")
        elif self._loopback:
            self._set_progress_text("Starting…")
            self._status_label.setText("Starting loopback sweep…")
        else:
            self._set_progress_text("Connecting…")
            self._status_label.setText("Connecting to Server…")

        # Clear stale smooth-progress state from any previous sweep /
        # previous segment.
        self._case_t0 = None
        self._active_case_position = None
        self._active_case_label = ""
        # Reset the whole-sweep ETA — it re-arms on the next
        # case_starting (per segment, by design).
        self._sweep_t0 = None
        self._sweep_eta_label.setText("")
        self._completed_case_walls = []

        # Treat "all 20" as None so we don't carry a redundant full-list
        # round-trip down through SweepWorker / ControlClient.
        full_run = len(selected) == self._sweep_table.total_count()
        worker_subset = None if full_run else selected

        host = self.ctx.run_state.server_host_override or str(self.ctx.config.network.server_ip)
        self._last_sweep_error = ""
        self._user_stopped = False
        self.ctx.run_state.connection_warning_text = ""
        window = self.window()
        if hasattr(window, "refresh_warning_banner"):
            window.refresh_warning_banner()
        worker = SweepWorker(
            self.ctx.config,
            server_host=host,
            selected_indexes=worker_subset,
            loopback=self._loopback,
        )
        worker.event.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
        worker.line_received.connect(self._on_line, Qt.ConnectionType.QueuedConnection)
        worker.sweep_finished.connect(self._on_sweep_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        # Stash the subset so _on_sweep_finished and the report sidecar
        # can record exactly what was requested vs actually ran.
        self._active_subset = list(selected)

        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        # Block role switching while the sweep is in flight.
        self.ctx.run_state.sweep_active = True
        worker.start()

    @Slot()
    def _on_stop(self) -> None:
        # Round-8 (Task GG, 2026-05-13): flag user-intent before we
        # tell the worker to stop, so the finished handler can pick
        # the right popup ("Stopped by user" vs "Aborted").
        self._user_stopped = True
        if self._worker is not None:
            self._worker.request_stop()
            self._stop_btn.setEnabled(False)
            self._status_label.setText("Stopping…")
        # Halt the per-case animation so the bar doesn't keep ticking
        # toward 95% while the worker is winding down.
        self._progress_timer.stop()
        self._sweep_eta_label.setText("")
        # Round-8 (Task GG, 2026-05-13): clear any stale connection-error
        # banner since this stop is user-initiated, not a real server
        # disconnect. Without this the orange top banner would linger
        # after Stop simply because run_sweep emitted an error event
        # while wrapping up the closed-socket case.
        self.ctx.run_state.connection_warning_text = ""
        window = self.window()
        if hasattr(window, "refresh_warning_banner"):
            window.refresh_warning_banner()

    def shutdown(self) -> None:
        """Stop the sweep worker + timers and block until the thread exits.

        Called by :class:`ScriptView` on teardown (role/config change
        rebuilds the Run tab; app close closes the window). The
        ``SweepWorker`` is not a Qt child of this panel, so without an
        explicit stop+wait a sweep in flight would outlive the panel —
        Qt logs ``QThread: Destroyed while thread is still running`` and
        the iperf3/fping subprocesses can be orphaned.
        """
        try:
            self._progress_timer.stop()
        except RuntimeError:
            pass
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.request_stop()
                # A case can be mid-iperf3; request_stop tears the
                # subprocesses down, but give the thread room to unwind.
                worker.wait(8000)
        except RuntimeError:
            # libshiboken: underlying C++ object already deleted.
            pass

    def _reset_chart(self) -> None:
        self._tp_data.clear()
        self._lat_data.clear()
        self._tp_curve.setData([], [])
        self._lat_curve.setData([], [])
        self._t0 = None

    @Slot(str, dict)
    def _on_event(self, name: str, data: dict) -> None:
        # Denominator for the progress label: the subset size if a
        # subset was selected, otherwise 20. ``self._active_subset`` is
        # populated in :meth:`_on_run`.
        total = (
            len(self._active_subset)
            if self._active_subset
            else self._sweep_table.total_count()
        )

        if name == "connecting":
            self._status_label.setText(
                f"Connecting to {data.get('host')}:{data.get('port')}…"
            )
        elif name == "connected":
            self._status_label.setText(
                f"Connected (server v{data.get('server_version', '?')})"
            )
        elif name == "case_starting":
            idx = int(data.get("case_idx", 0))
            self._sweep_table.mark_running(idx)
            # Position-in-subset, not absolute index in the 20-case grid.
            position = self._position_in_subset(idx)
            self._reset_chart()

            # Kick off smooth-progress tracking for this case.
            self._case_t0 = time.monotonic()
            self._active_case_position = position
            self._active_case_label = str(data.get("case", ""))
            self._active_case_duration_s = self.ctx.config.test_plan.duration_s
            # Arm the whole-sweep clock on the first case of the sweep
            # / segment; later cases leave it running.
            if self._sweep_t0 is None:
                self._sweep_t0 = time.monotonic()

            # Snap the bar forward immediately to the start-of-case
            # mark, then the timer fills in the per-second motion. The
            # denominator is the *full* per-case wall time, not just
            # the iperf3 duration — see :meth:`_on_progress_tick` for
            # why.
            case_total_s = int(self._per_case_estimate_s())
            self._progress.setValue((position - 1) * 100)
            self._set_progress_text(
                f"Case {position}/{total}: {self._active_case_label} · "
                f"0s/{case_total_s}s"
            )
            if not self._progress_timer.isActive():
                self._progress_timer.start()
            # Paint the bar + ETA readout immediately rather than
            # waiting up to 250 ms for the first timer tick.
            self._on_progress_tick()
        elif name == "case_done":
            idx = int(data.get("case_idx", 0))
            position = self._position_in_subset(idx)
            # Stop the per-case timer and snap to the next mark — the
            # short overhead between cases (server warmup, ~3 s) is
            # below the user's perception threshold so a clean snap is
            # cleaner than trying to animate it.
            self._progress_timer.stop()
            self._progress.setValue(position * 100)
            # Record this case's real wall time so the ETA for the
            # remaining cases is measured, not guessed (QQ — Round 18).
            if self._case_t0 is not None:
                self._completed_case_walls.append(
                    time.monotonic() - self._case_t0
                )
            self._case_t0 = None
            self._active_case_position = None

            ok = bool(data.get("ok", False))
            entry = data.get("entry")
            if entry is not None:
                # Fill in the row's metric columns immediately so the user
                # sees results accumulate live, not just at the very end.
                self._sweep_table.mark_done(entry)
            self._set_progress_text(
                f"Case {position}/{total} done — {'ok' if ok else 'errors'}"
            )
            self._status_label.setText(
                f"Case {position}/{total} finished — {'ok' if ok else 'with errors'}"
            )
        elif name == "sweep_finished":
            self._progress_timer.stop()
            self._sweep_eta_label.setText("")
            self._status_label.setText(
                f"Sweep complete: {data.get('cases', 0)} cases."
            )
        elif name == "error":
            self._progress_timer.stop()
            self._sweep_eta_label.setText("")
            msg = str(data.get("message", "unknown"))
            self._last_sweep_error = msg
            self.ctx.run_state.connection_warning_text = (
                f"Server connection error: {msg}"
            )
            # Refresh the top banner so the warning is visible across
            # every tab, not just the Run tab. (Task T.)
            window = self.window()
            if hasattr(window, "refresh_warning_banner"):
                window.refresh_warning_banner()
            self._status_label.setText(f"ERROR: {msg}")
            self.ctx.logger.error("sweep error event: %s", msg)

    def _per_case_estimate_s(self) -> float:
        """Best estimate of one case's wall-clock time, in seconds.

        Uses the measured average of cases already finished in this
        sweep / segment; before the first case completes it falls back
        to the static model in
        :func:`core.runner.estimate_case_wall_s`. Measuring makes the
        ETA self-correcting — accurate regardless of ``duration_s``,
        the fping interval, or machine speed (QQ — Round 18).
        """
        if self._completed_case_walls:
            return sum(self._completed_case_walls) / len(self._completed_case_walls)
        return estimate_case_wall_s(
            float(self._active_case_duration_s),
            float(self.ctx.config.fping.interval_ms),
        )

    @Slot()
    def _on_progress_tick(self) -> None:
        """Animate the per-sweep progress bar within a single case.

        Fires every 250 ms while a case is in flight. Computes a
        fractional position from elapsed monotonic time vs the case's
        estimated wall-clock budget (see :meth:`_per_case_estimate_s`).
        Capped at 95 % so the bar never looks finished before the
        SERVER_RESULT actually lands. At case_done the handler in
        :meth:`_on_event` snaps to the full per-case mark and stops
        this timer.
        """
        if self._case_t0 is None or self._active_case_position is None:
            return
        elapsed = time.monotonic() - self._case_t0
        # Per-case wall budget — adaptive: the measured average of
        # finished cases once any exist, else the duration-aware static
        # model. fping (running at the Windows timer granularity) is
        # what actually bounds the case, well above the bare iperf3
        # ``duration_s``.
        case_total_s = max(1.0, self._per_case_estimate_s())
        # Cap at 0.95 — the iperf3-server's exit + the SERVER_RESULT
        # round-trip can occasionally outrun the empirical overhead,
        # and showing the bar at 100% before case_done would lie.
        frac = min(0.95, elapsed / case_total_s)
        position = self._active_case_position
        value = int((position - 1) * 100 + frac * 100)
        self._progress.setValue(value)

        total = (
            len(self._active_subset)
            if self._active_subset
            else self._sweep_table.total_count()
        )
        elapsed_s = int(elapsed)
        self._set_progress_text(
            f"Case {position}/{total}: {self._active_case_label} · "
            f"{elapsed_s}s/{int(case_total_s)}s"
        )

        # --- whole-sweep ETA readout (Feature 1) ---
        # elapsed = wall-clock since the sweep's first case_starting.
        # left = the in-flight case's leftover time plus a full
        # per-case budget for every case still queued behind it:
        #   remaining_cases × case_total − case_elapsed
        # (remaining_cases counts the in-flight case itself).
        if self._sweep_t0 is not None:
            sweep_elapsed = time.monotonic() - self._sweep_t0
            sweep_left = sweep_time_left_s(
                position=position,
                total=total,
                case_total_s=case_total_s,
                case_elapsed_s=elapsed,
            )
            self._sweep_eta_label.setText(
                f"Sweep  {fmt_duration(sweep_elapsed)} elapsed  ·  "
                f"~{fmt_duration(sweep_left)} left"
            )

    def _position_in_subset(self, case_idx: int) -> int:
        """Return ``case_idx``'s 1-based position within the active subset.

        With no subset, this is just ``case_idx`` (1..20). With a subset
        of e.g. [3, 7, 12], case 7 is position 2.
        """
        if not self._active_subset:
            return case_idx
        try:
            return self._active_subset.index(case_idx) + 1
        except ValueError:
            return case_idx

    @Slot(str, str)
    def _on_line(self, source: str, line: str) -> None:
        if source == "iperf3-server":
            return
        self._log.appendPlainText(f"[{source}] {line}")

        if self._t0 is None:
            self._t0 = time.monotonic()
        elapsed = time.monotonic() - self._t0

        m = _FPING_LIVE_RE.search(line)
        if m:
            rtt = float(m.group("rtt"))
            self._lat_data.append((elapsed, rtt))
            xs = [p[0] for p in self._lat_data[-10000:]]
            ys = [p[1] for p in self._lat_data[-10000:]]
            self._lat_curve.setData(xs, ys)
            return

        if source == "iperf3-client":
            for s in parse_intervals(line):
                self._tp_data.append((s.end_s, s.throughput_mbps))
            xs = [p[0] for p in self._tp_data[-10000:]]
            ys = [p[1] for p in self._tp_data[-10000:]]
            self._tp_curve.setData(xs, ys)

    @Slot(object)
    def _on_sweep_finished(self, sweep: SweepResult) -> None:
        # Defensive: stop the per-case animation timer in case the
        # sweep ended via an error path that bypassed case_done.
        self._progress_timer.stop()

        # Fill in any per-case rows that came back via SweepResult but
        # weren't already updated through case_done events.
        for entry in sweep.cases:
            self._sweep_table.mark_done(entry)

        # Make the result visible to other tabs (Save Options tab reads from here).
        self.ctx.run_state.last_sweep_result = sweep

        ok = sum(1 for e in sweep.cases if e.ok)
        total = len(sweep.cases)
        # Only snap the bar to 100% when the sweep actually ran cases.
        # A 0-case finish is handled by _reset_after_zero_case_finish
        # below; jumping to 100% first would flicker the bar full→empty.
        if total > 0:
            self._progress.setValue(self._progress.maximum())

        # Common bookkeeping for both single-sweep and multi-segment paths.
        self._stop_btn.setEnabled(False)
        # Detach the just-finished worker's data signals before we drop
        # our reference. In continuous mode the next segment builds a
        # fresh SweepWorker wired to these same slots; leaving the dead
        # worker's connections live risks a late queued event/line landing
        # in the new sweep's handlers. ``finished`` → ``deleteLater`` is a
        # separate signal and stays connected so the object still frees.
        finished_worker = self._worker
        if finished_worker is not None:
            for sig in (
                finished_worker.event,
                finished_worker.line_received,
                finished_worker.sweep_finished,
            ):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        self._worker = None

        if self._multi_in_progress:
            self._handle_multi_segment_finished(sweep, ok, total)
        else:
            self._handle_single_sweep_finished(sweep, ok, total)

    def _kickoff_next_segment(self) -> None:
        """Start the next continuous-mode segment on a clean event-loop tick.

        Invoked via ``QTimer.singleShot(0, …)`` from the between-segments
        dialog dispatch so the fresh :class:`SweepWorker` is created only
        after :meth:`_on_sweep_finished` has fully unwound and the previous
        worker's ``deleteLater`` has been processed — starting a new
        QThread from inside the old thread's terminal slot, across the
        dialog's nested event loop, is fragile.
        """
        # Keep the Run-tab label field showing the segment now starting.
        # CONTINUE / RETRY / SKIP all funnel through here, so this single
        # update covers every advance — the field no longer stays frozen
        # on segment 1's text.
        self._segment_label_edit.setText(self._current_segment_label)
        self._start_sweep_worker(self._active_subset)

    def _handle_single_sweep_finished(
        self, sweep: SweepResult, ok: int, total: int,
    ) -> None:
        """Phase 4 single-sweep finish path, with Group C-1 follow-up
        save flow: prompt-first by default, auto-save when the user
        has opted in via the dialog's 'Don't ask me in the future'."""
        # If the sweep aborted with a control-channel / protocol error,
        # surface it prominently — both as the progress-bar label and a
        # modal popup — so it doesn't get hidden behind the
        # "Done — 0/0 ok" cosmetic finish. (Task F, 2026-05-12.)
        # Three classes of zero-case finish, each with its own messaging:
        #   1. User pressed Stop          → "Stopped by user"
        #   2. Connection / protocol error → "Sweep aborted — <reason>"
        #   3. Plain 0-case empty result   → falls through to normal flow
        # (Tasks L + M, 2026-05-12.)
        if total == 0 and self._user_stopped:
            self._set_progress_text("Stopped by user — no cases recorded")
            self._status_label.setText("Sweep stopped by user.")
            QMessageBox.information(
                self, "Sweep stopped",
                "You stopped the sweep before any case completed. "
                "Nothing was saved.",
            )
            self._reset_after_zero_case_finish()
            return

        if self._last_sweep_error and total == 0:
            err_msg = self._last_sweep_error
            self._set_progress_text(f"Sweep aborted — {err_msg[:60]}")
            self._status_label.setText(f"Sweep aborted: {err_msg}")
            _notify_sound(SoundEvent.ERROR)  # Round-6 #7
            show_error_with_help(
                self, self.ctx, "Sweep aborted",
                "The sweep could not complete:\n\n"
                f"{err_msg}\n\n"
                "Common causes:\n"
                "  · Server PC's IP is wrong for its role — fix via Setup tab\n"
                "  · Server PC isn't running PingPair, or the Run tab\n"
                "    Server is still starting up\n"
                "  · Firewall blocking port 5202 — re-run the Setup checks\n"
                "  · WinError 10049 'address not valid' means the Server\n"
                "    tried to bind an IP that isn't on any local NIC; fix\n"
                "    its Local NIC IP and restart both sides.",
            )
            self._reset_after_zero_case_finish()
            return

        # Task V (2026-05-13): partial Stop — Server was reachable and some
        # cases ran before the user clicked Stop. Show a friendly popup,
        # offer to save the partial data, and reset the bar regardless.
        if self._user_stopped and total > 0:
            self._set_progress_text(
                f"Stopped by user — {ok}/{total} cases recorded so far"
            )
            self._status_label.setText(
                f"Sweep stopped by user after {ok}/{total} cases."
            )
            choice = QMessageBox.question(
                self, "Sweep stopped",
                f"You stopped the sweep after {ok}/{total} cases completed.\n\n"
                f"Save the partial data anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes:
                # Auto-save the partial data with the current Save settings —
                # off the GUI thread (behind the "Saving report…" indicator) so
                # the write doesn't freeze the window.
                _written, save_err = self._auto_save_single(
                    sweep, selected_indexes=list(self._active_subset),
                )
                if save_err:
                    self._status_label.setText(
                        f"Sweep stopped — {ok}/{total} cases · ⚠ save failed: {save_err}"
                    )
                else:
                    self._status_label.setText(
                        f"Sweep stopped — {ok}/{total} cases saved."
                    )
            else:
                self._status_label.setText(
                    f"Sweep stopped — {ok}/{total} cases · not saved."
                )
            self._active_subset = []
            self._reset_after_zero_case_finish()
            return

        # Distinguish a clean finish from a partial-error finish.
        # If the sweep emitted an error event AND some cases ran, the
        # "Done" label is misleading — the sweep aborted but we
        # already have data. Show "finished with errors" instead.
        # (Task Q, 2026-05-12.)
        if self._last_sweep_error:
            self._set_progress_text(
                f"Sweep finished with errors — {ok}/{total} ok"
            )
        else:
            self._set_progress_text(f"Done — {ok}/{total} ok")
        duration_text = fmt_duration(sweep.duration_s)
        result_summary = f"{ok}/{total} cases ok · {duration_text}"
        if self._last_sweep_error:
            result_summary += f" · ⚠ {self._last_sweep_error}"

        rs = self.ctx.run_state
        # Stash these before we hand back control — both the dialog
        # path and the popup path need them and the panel-level
        # cleanup below would otherwise zero out _active_subset.
        active_subset_snapshot = list(self._active_subset)
        self._active_subset = []

        # Re-enable interactivity straight away so the user can flip
        # subset / continuous-mode while the save dialog is up.
        self.ctx.run_state.sweep_active = False
        self._sweep_table.set_interactive(True)
        self._on_selection_changed(self._sweep_table.selected_count())

        # Round-6 #7: one outcome sound per finished sweep, fired here so it
        # plays in BOTH the auto-save and prompt-first paths (and before any
        # following save dialog, so there's no double beep). Skipped for a
        # degenerate 0-case finish, which has no real outcome to announce.
        if total > 0:
            clean = ok == total and not self._last_sweep_error
            _notify_sound(SoundEvent.SUCCESS if clean else SoundEvent.FAILURE)

        if rs.report_auto_save and total > 0:
            # Hands-free path — same shape as today: auto-save, then
            # show the result popup with saved-files info.
            written, save_err = self._auto_save_single(
                sweep, selected_indexes=active_subset_snapshot,
            )
            msg = f"Sweep finished in {duration_text} — {ok}/{total} cases ok"
            if written:
                save_dir = written[0].parent
                msg += f"  ·  saved {len(written)} report file(s) to {save_dir}"
            elif save_err:
                msg += f"  ·  ⚠ auto-save failed: {save_err}"
            self._status_label.setText(msg)
            self._show_sweep_finished_popup(
                sweep, ok, total, duration_text, written,
            )
            self._reset_progress_to_ready()
            return

        if total == 0:
            # Defensive: a 0-case run is degenerate — no point asking
            # to save an empty report. (Task J, 2026-05-12.)
            self._status_label.setText(
                "Sweep finished with 0 cases — nothing to save."
            )
            return

        # Prompt-first path — show the post-test save dialog.
        result = self._prompt_save_dialog(
            result_summary=result_summary,
            is_multi_segment=False,
        )
        self._dispatch_save_dialog_single(
            result=result,
            sweep=sweep,
            selected_indexes=active_subset_snapshot,
            duration_text=duration_text,
            ok=ok, total=total,
        )


    def _reset_after_zero_case_finish(self) -> None:
        """Restore the panel to its idle/ready state after a 0-case finish.

        Used after "Sweep aborted" and "Sweep stopped by user" popups
        so the user sees a clean panel ready for the next attempt rather
        than a stale red-error progress label and "Sweep aborted: …"
        status text from the previous run. (Task L, 2026-05-12.)
        """
        self.ctx.run_state.sweep_active = False
        self._sweep_table.set_interactive(True)
        self._worker = None
        # Round-19 (VV): re-evaluate the Run button's label + enabled
        # state now that the worker is cleared. _start_sweep_worker
        # disabled the button at sweep start; every clean-finish path
        # re-enables it via _on_selection_changed, but the abort / user-
        # stop / partial-stop paths route here instead and used to skip
        # that call — so after a connection failure the "Run subset"
        # button stayed greyed and unclickable until the user toggled a
        # row (which fired selection_changed). Calling it here restores
        # the button immediately on every zero-case finish.
        self._on_selection_changed(self._sweep_table.selected_count())
        # Reset progress bar + format back to idle. The next sweep will
        # re-set both when it starts.
        try:
            self._progress.setValue(0)
            self._set_progress_text("Ready")
        except Exception:  # noqa: BLE001
            pass
        # Clear the lingering error so the next sweep starts fresh.
        self._last_sweep_error = ""
        self._user_stopped = False
        # Restore the operator's original first-segment label so the field
        # no longer shows the last segment's name from the run that just
        # ended (it becomes the segment-1 label for the next run). No-op
        # for single sweeps — the field is hidden there.
        self._segment_label_edit.setText(self._first_segment_label_text)

    def _reset_progress_to_ready(self) -> None:
        """Reset the progress bar back to 0/Ready after a finish.

        Called from every finish path (auto-save, user-save, skip,
        abort) so the bar doesn't carry the previous run's label
        into the next attempt. The status_label keeps the
        result text — only the progress bar is reset.
        (Task R, 2026-05-12.)
        """
        try:
            self._progress.setValue(0)
            self._set_progress_text("Ready")
        except Exception:  # noqa: BLE001
            pass

    def _save_in_background(
        self, save_fn: "Callable[[], list[Path]]",
    ) -> tuple[list[Path], str]:
        """Run a report-writing ``save_fn`` off the GUI thread, returning
        ``(written, error)``. Thin wrapper over the shared
        :func:`run_save_in_background` (also used by the Save Options manual
        save and the Analysis comparison export) so the busy-indicator +
        worker-teardown logic lives in one place. (2026-06-02; shared 2026-06-04.)
        """
        return run_save_in_background(self, save_fn, logger=self.ctx.logger)

    def _auto_save_single(
        self,
        sweep: SweepResult,
        *,
        selected_indexes: list[int],
    ) -> tuple[list[Path], str]:
        """Save a single sweep's report set off the GUI thread. (written, err)."""
        from .report_view import _save_sweep

        return self._save_in_background(
            lambda: _save_sweep(
                self.ctx, sweep, selected_indexes=selected_indexes,
            )
        )

    def _prompt_save_dialog(
        self,
        *,
        result_summary: str,
        is_multi_segment: bool,
    ) -> SaveDialogResult:
        """Show the post-test save dialog. Returns the operator's choice."""
        rs = self.ctx.run_state
        dlg = SaveReportDialog(
            self,
            result_summary=result_summary,
            default_destination=rs.report_dir,
            default_pattern=rs.report_filename_pattern,
            is_multi_segment=is_multi_segment,
        )
        dlg.exec()
        return dlg.collect_result()

    def _dispatch_save_dialog_single(
        self,
        *,
        result: SaveDialogResult,
        sweep: SweepResult,
        selected_indexes: list[int],
        duration_text: str,
        ok: int,
        total: int,
    ) -> None:
        """Apply the operator's choice from the save dialog (single sweep)."""
        rs = self.ctx.run_state
        if result.decision is SaveDialogDecision.SKIP:
            # Clean close — leave a status hint so the user knows the
            # in-memory SweepResult is still there for the Save Options tab's
            # manual Save report now button.
            self._status_label.setText(
                f"Sweep finished — {ok}/{total} cases ok · "
                f"{duration_text}  ·  Run not saved · click "
                "Save report now on the Save Options tab if you change your mind."
            )
            # Round-7 (Task CC, 2026-05-13): reset the bar even on Skip
            # so the next sweep doesn't carry over the previous run's
            # 100% green/error label.
            self._reset_progress_to_ready()
            return

        # SAVE — mirror the chosen Destination + Pattern back into
        # RunState before invoking the save helper, so the helper uses
        # the operator's per-run override. If the user opted into
        # 'Don't ask me in the future', also flip Auto save on; the
        # chosen values become the new defaults.
        rs.report_dir = result.destination_dir
        rs.report_filename_pattern = result.filename_pattern
        if result.remember:
            rs.report_auto_save = True
        # Push the new values back to the Save Options tab's widgets so the
        # operator's choices are reflected immediately — no tab
        # switch required.
        self.ctx.notify_save_settings_changed()

        written, save_err = self._auto_save_single(
            sweep, selected_indexes=selected_indexes,
        )
        if written:
            msg = (
                f"Sweep finished in {duration_text} — {ok}/{total} cases ok"
                f"  ·  saved {len(written)} report file(s) to {written[0].parent}"
            )
            if result.remember:
                msg += "  ·  Auto save now on"
            self._status_label.setText(msg)
        else:
            self._status_label.setText(
                f"Sweep finished in {duration_text} — {ok}/{total} cases ok"
                f"  ·  ⚠ save failed: {save_err}"
            )
        self._reset_progress_to_ready()

    def _handle_multi_segment_finished(
        self, sweep: SweepResult, ok: int, total: int,
    ) -> None:
        """Group C-1: between-segments dispatch.

        Wraps the just-finished sweep into a :class:`SweepSegment`,
        appends it to :attr:`_multi_segments`, and pops the
        between-segments dialog. The operator's choice routes us to
        Continue (start next segment), Retry (re-run same segment),
        or Save (finalise + write multi-segment report).
        """
        # ---- Early abort detection (Task O, 2026-05-12) ----
        # Before showing the between-segments dialog, check whether
        # the segment finished due to a user Stop or a connection
        # failure. The previous version showed the between-segments
        # dialog unconditionally, which meant a Server-unreachable
        # continuous run looked like a normal segment-failed result
        # — and the final save prompt asked to save an empty run.
        if self._user_stopped:
            self._set_progress_text("Stopped by user — no cases recorded")
            self._status_label.setText(
                f"Continuous-mode sweep stopped by user during "
                f"segment {self._current_segment_idx}."
            )
            QMessageBox.information(
                self, "Continuous-mode sweep stopped",
                "You stopped the continuous run. Any segments already "
                "completed are retained in memory but not yet saved.\n\n"
                "Click Save report now on the Save Options tab if you want "
                "to keep what was collected.",
            )
            # Reset multi-segment state but keep the segments list so
            # the user can still trigger a manual save if they want.
            self._multi_in_progress = False
            self._current_segment_label = ""
            self._current_segment_idx = 0
            self._continuous_check.setEnabled(True)
            self._segment_label_edit.setEnabled(True)
            self._reset_after_zero_case_finish()
            return

        if self._last_sweep_error and total == 0:
            err_msg = self._last_sweep_error
            self._set_progress_text(f"Sweep aborted — {err_msg[:60]}")
            self._status_label.setText(f"Continuous-mode sweep aborted: {err_msg}")
            _notify_sound(SoundEvent.ERROR)  # Round-6 #7 (parity with single-sweep abort)
            show_error_with_help(
                self, self.ctx, "Sweep aborted",
                "The continuous-mode run could not complete:\n\n"
                f"{err_msg}\n\n"
                "Common causes:\n"
                "  · Server PC's IP is wrong for its role — fix via Setup tab\n"
                "  · Server PC isn't running PingPair, or its Run tab\n"
                "    Server is still starting up\n"
                "  · Firewall blocking port 5202 — re-run the Setup checks\n"
                "  · WinError 10049 'address not valid' means the Server\n"
                "    tried to bind an IP that isn't on any local NIC; fix\n"
                "    its Local NIC IP and restart both sides.",
            )
            self._multi_in_progress = False
            self._multi_segments = []
            self._current_segment_label = ""
            self._current_segment_idx = 0
            self._continuous_check.setEnabled(True)
            self._segment_label_edit.setEnabled(True)
            self._reset_after_zero_case_finish()
            return

        # Classify the segment so the dialog can disable Retry when
        # the segment finished cleanly.
        if total == 0:
            seg_status = "failed"
            seg_error = "no cases ran (control-channel error?)"
        elif ok == total:
            seg_status = "ok"
            seg_error = ""
        elif ok > 0:
            seg_status = "partial"
            seg_error = f"{total - ok} of {total} cases errored"
        else:
            seg_status = "failed"
            seg_error = "every case errored"

        segment = SweepSegment(
            segment_idx=self._current_segment_idx,
            label=self._current_segment_label,
            sweep=sweep,
            status=seg_status,
            error=seg_error,
        )
        self._multi_segments.append(segment)

        seg_duration = fmt_duration(sweep.duration_s)
        self._set_progress_text(
            f"Segment {self._current_segment_idx} done — "
            f"{ok}/{total} {seg_status}"
        )
        seg_name = segment_display_name(
            self._current_segment_idx, self._current_segment_label
        )
        self._status_label.setText(
            f"{seg_name}: {ok}/{total} cases · {seg_status} · {seg_duration}"
        )

        # Show the dialog and dispatch on the operator's choice.
        _notify_sound(SoundEvent.PROMPT)  # Round-6 #7: a decision is needed
        dlg = BetweenSegmentsDialog(
            self,
            completed_segments=self._multi_segments,
            last_segment_status=seg_status,
        )
        dlg.exec()
        result = dlg.collect_result()

        if result.decision is SegmentDecision.CONTINUE:
            # Advance the segment counter and kick off the next sweep.
            self._current_segment_idx += 1
            self._current_segment_label = (
                result.next_label
                or f"Segment {self._current_segment_idx}"
            )
            QTimer.singleShot(0, self._kickoff_next_segment)
            return

        if result.decision is SegmentDecision.RETRY:
            # Drop the just-appended segment and re-run with the same
            # idx + label. The retried run produces a fresh
            # SweepSegment that will be appended in its place.
            self._multi_segments.pop()
            QTimer.singleShot(0, self._kickoff_next_segment)
            return

        if result.decision is SegmentDecision.SKIP:
            # Task U (2026-05-13): keep the just-finished segment in the
            # report (so the failure is auditable) and advance to the
            # next segment without retrying. Same kickoff path as
            # Continue, but no fresh label prompt needed.
            self._current_segment_idx += 1
            self._current_segment_label = (
                result.next_label
                or f"Segment {self._current_segment_idx}"
            )
            QTimer.singleShot(0, self._kickoff_next_segment)
            return

        # SegmentDecision.SAVE — finalise the multi-segment run.
        self._finalize_multi_segment_run()

    def _finalize_multi_segment_run(self) -> None:
        """Roll _multi_segments into a MultiSweepResult and save.

        Routes to either the auto-save path (today's hands-free behaviour
        + result popup) or the post-test save dialog depending on the
        Save Options tab's Auto save toggle.
        """
        multi = MultiSweepResult(
            started_at=self._multi_started_at,
            ended_at=time.time(),
            segments=list(self._multi_segments),
            selected_case_indexes=list(self._active_subset),
        )

        duration_text = fmt_duration(multi.duration_s)
        result_summary = (
            f"{multi.segments_ok}/{multi.segments_total} segments ok · "
            f"{multi.total_cases_ok}/{multi.total_cases} cases ok · "
            f"{duration_text}"
        )

        self._set_progress_text(
            f"Done — {multi.segments_ok}/{multi.segments_total} segments ok"
        )

        # Common cleanup BEFORE save / prompt — the dialog should not
        # be blocked by sweep_active=True and the operator may want to
        # tweak subset / continuous-mode mid-dialog.
        self._multi_in_progress = False
        self._multi_segments = []
        self._current_segment_label = ""
        self._current_segment_idx = 0
        self._continuous_check.setEnabled(True)
        self._segment_label_edit.setEnabled(True)
        # Restore the operator's original first-segment label (the field
        # was tracking the live segment during the run).
        self._segment_label_edit.setText(self._first_segment_label_text)
        self.ctx.run_state.sweep_active = False
        self._sweep_table.set_interactive(True)
        self._active_subset = []
        self._on_selection_changed(self._sweep_table.selected_count())

        rs = self.ctx.run_state
        if rs.report_auto_save and multi.segments_total > 0:
            # Hands-free path — same shape as today: auto-save, then
            # show the result popup with saved-files info.
            written, save_err = self._auto_save_multi(multi)
            msg = (
                f"Multi-segment run finished in {duration_text} — "
                f"{multi.segments_ok}/{multi.segments_total} segments ok, "
                f"{multi.total_cases_ok}/{multi.total_cases} cases ok"
            )
            if written:
                msg += f"  ·  saved {len(written)} report file(s) to {written[0].parent}"
            elif save_err:
                msg += f"  ·  ⚠ auto-save failed: {save_err}"
            self._status_label.setText(msg)
            self._show_multi_segment_finished_popup(multi, written)
            self._reset_progress_to_ready()
            return

        # Bail before the save dialog if there's nothing worth saving:
        #   - no segments attempted, OR
        #   - segments attempted but ALL failed before producing any case
        #     data (typical when the Server was unreachable).
        # Saving an empty multi-segment report is degenerate and would
        # mislead the user into thinking they had data.
        # (Task J, 2026-05-12.)
        if multi.segments_total == 0 or multi.total_cases == 0:
            if multi.segments_total == 0:
                msg = ("Multi-segment run finished with no segments — "
                       "nothing to save.")
            else:
                msg = ("Multi-segment run produced 0 cases across "
                       f"{multi.segments_total} segment(s) — nothing to save.")
            self._status_label.setText(msg)
            return

        # Prompt-first path — show the save dialog.
        result = self._prompt_save_dialog(
            result_summary=result_summary,
            is_multi_segment=True,
        )
        self._dispatch_save_dialog_multi(
            result=result, multi=multi, duration_text=duration_text,
        )

    def _auto_save_multi(
        self, multi: MultiSweepResult,
    ) -> tuple[list[Path], str]:
        """Save a multi-segment report set off the GUI thread. (written, err)."""
        from .report_view import _save_multi_sweep

        return self._save_in_background(lambda: _save_multi_sweep(self.ctx, multi))

    def _dispatch_save_dialog_multi(
        self,
        *,
        result: SaveDialogResult,
        multi: MultiSweepResult,
        duration_text: str,
    ) -> None:
        """Apply the operator's choice from the save dialog (multi-segment)."""
        rs = self.ctx.run_state
        if result.decision is SaveDialogDecision.SKIP:
            self._status_label.setText(
                f"Multi-segment run finished in {duration_text} — "
                f"{multi.segments_ok}/{multi.segments_total} segments ok, "
                f"{multi.total_cases_ok}/{multi.total_cases} cases ok  ·  "
                "Run not saved · click Save report now on the Save Options tab "
                "if you change your mind."
            )
            # Round-7 (Task CC, 2026-05-13): reset the bar even on Skip
            # so the next sweep doesn't carry over the previous run's
            # 100% green/error label.
            self._reset_progress_to_ready()
            return

        rs.report_dir = result.destination_dir
        rs.report_filename_pattern = result.filename_pattern
        if result.remember:
            rs.report_auto_save = True
        self.ctx.notify_save_settings_changed()

        written, save_err = self._auto_save_multi(multi)
        if written:
            msg = (
                f"Multi-segment run finished in {duration_text} — "
                f"{multi.segments_ok}/{multi.segments_total} segments ok, "
                f"{multi.total_cases_ok}/{multi.total_cases} cases ok  ·  "
                f"saved {len(written)} report file(s) to {written[0].parent}"
            )
            if result.remember:
                msg += "  ·  Auto save now on"
            self._status_label.setText(msg)
        else:
            self._status_label.setText(
                f"Multi-segment run finished in {duration_text} — "
                f"{multi.segments_ok}/{multi.segments_total} segments ok"
                f"  ·  ⚠ save failed: {save_err}"
            )
        self._reset_progress_to_ready()

    def _show_multi_segment_finished_popup(
        self, multi: MultiSweepResult, written: list[Path],
    ) -> None:
        """Modal summary of the multi-segment run."""
        box = QMessageBox(self)
        if (multi.segments_ok == multi.segments_total
                and multi.total_cases_ok == multi.total_cases):
            box.setIcon(QMessageBox.Icon.Information)
            title = "Multi-segment run complete"
            _notify_sound(SoundEvent.SUCCESS)
        else:
            box.setIcon(QMessageBox.Icon.Warning)
            title = "Multi-segment run finished with errors"
            _notify_sound(SoundEvent.FAILURE)
        box.setWindowTitle(title)
        box.setText(
            f"<b>{multi.segments_ok}/{multi.segments_total} segments ok</b><br>"
            f"<b>{multi.total_cases_ok}/{multi.total_cases} cases ok</b><br>"
            f"Total duration: {fmt_duration(multi.duration_s)}"
        )
        if written:
            box.setInformativeText(
                f"Saved {len(written)} report file(s) to:<br>"
                f"<code>{written[0].parent}</code>"
            )
            details = "\n".join(str(p) for p in written)
            box.setDetailedText(details)
            widen_detailed_box(box)
        box.exec()

    def _show_sweep_finished_popup(
        self,
        sweep: SweepResult,
        ok: int,
        total: int,
        duration_text: str,
        written: list[Path],
    ) -> None:
        """Modal summary of the single-sweep result."""
        box = QMessageBox(self)
        if ok == total and total > 0:
            box.setIcon(QMessageBox.Icon.Information)
            title = "Sweep complete"
        else:
            box.setIcon(QMessageBox.Icon.Warning)
            title = "Sweep finished with errors"
        box.setWindowTitle(title)
        box.setText(
            f"<b>{ok}/{total} cases ok</b><br>"
            f"Total duration: {duration_text}"
        )
        if written:
            box.setInformativeText(
                f"Saved {len(written)} report file(s) to:<br>"
                f"<code>{written[0].parent}</code>"
            )
            details = "\n".join(str(p) for p in written)
            box.setDetailedText(details)
            widen_detailed_box(box)
        box.exec()
