"""Ping tab — quick reachability check before launching a full sweep.

Wraps Windows' built-in ``ping.exe`` (universally available, no Cygwin
required) under a small Qt form: target IP, packet count, live output,
and a parsed summary on completion.
"""

from __future__ import annotations

import re
import subprocess
import sys

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..context import Role
from ..core.runner import _NO_WINDOW
from ._base import BaseView
from ._validators import attach_ipv4


# Windows ping summary lines we extract for the metrics panel.
#   Ping statistics for 192.168.1.1:
#       Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),
#   Approximate round trip times in milli-seconds:
#       Minimum = 0ms, Maximum = 2ms, Average = 0ms
_PACKETS_RE = re.compile(
    r"Sent\s*=\s*(?P<sent>\d+)\s*,\s*Received\s*=\s*(?P<rcv>\d+)\s*,\s*Lost\s*=\s*(?P<lost>\d+).*?\((?P<lossp>\d+)%"
)
_RTT_RE = re.compile(
    r"Minimum\s*=\s*(?P<min>\d+)ms\s*,\s*Maximum\s*=\s*(?P<max>\d+)ms\s*,\s*Average\s*=\s*(?P<avg>\d+)ms"
)


class _PingWorker(QThread):
    """Run ``ping.exe`` and stream its stdout to the UI."""

    line = Signal(str)
    finished_ok = Signal(int, str)  # (returncode, full_stdout)

    def __init__(self, target: str, count: int) -> None:
        super().__init__()
        self.target = target
        self.count = count
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:  # noqa: D401
        # ``-n N`` on Windows; ``-c N`` on POSIX.  We want the same
        # control flow either way for testability on a dev box.
        if sys.platform == "win32":
            argv = ["ping", "-n", str(self.count), self.target]
        else:
            argv = ["ping", "-c", str(self.count), self.target]

        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            self.finished_ok.emit(-1, f"could not start ping: {exc}")
            return

        chunks: list[str] = []
        assert self._proc.stdout is not None
        for raw in iter(self._proc.stdout.readline, ""):
            chunks.append(raw)
            self.line.emit(raw.rstrip("\r\n"))
        rc = self._proc.wait()
        self.finished_ok.emit(rc, "".join(chunks))

    def request_stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            # Escalate to kill if ping.exe ignores the terminate — without
            # this the run() thread's readline loop could block forever and
            # finished_ok would never fire, leaving Stop stuck disabled.
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass


class PingView(BaseView):
    title = "Ping — reachability"

    def _build_placeholder(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        outer.addWidget(QLabel(f"<h2>{self.title}</h2>"))
        _intro = QLabel(
            "Quick reachability check using Windows' built-in ping. Useful "
            "as a smoke test before launching the full 20-case sweep — if "
            "this fails, fix the network or the firewall first."
        )
        _intro.setWordWrap(True)
        outer.addWidget(_intro)

        # ----- form -----
        form_box = QGroupBox("Parameters")
        form = QFormLayout(form_box)

        # Default target = the OTHER side, by role: a Server pings the Client
        # (192.168.1.2), a Client pings the Server it sweeps against
        # (192.168.1.1 or the host override), Loopback pings 127.0.0.1.
        # Stored in _auto_target so refresh() can re-default it after a role
        # change without ever clobbering an IP the user typed themselves.
        self._auto_target = self._default_target()
        self._target_edit = QLineEdit(self._auto_target)
        # Live red-border feedback on invalid IPv4 (Group F follow-up).
        # ping.exe itself will refuse a bad target with a clear error,
        # but the visual cue saves a round-trip through the subprocess.
        attach_ipv4(
            self._target_edit,
            "Target IPv4 address (dotted-quad), e.g. 192.168.1.1.",
            allow_blank=False,
        )
        form.addRow("Target IP:", self._target_edit)

        self._count_spin = QSpinBox()
        self._count_spin.setRange(1, 100)
        self._count_spin.setValue(4)
        form.addRow("Count (packets):", self._count_spin)

        outer.addWidget(form_box)

        # ----- control row -----
        ctl = QHBoxLayout()
        self._ping_btn = QPushButton("Ping")
        self._ping_btn.clicked.connect(self._on_ping)
        ctl.addWidget(self._ping_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        ctl.addWidget(self._stop_btn)
        ctl.addStretch(1)
        outer.addLayout(ctl)

        # ----- output -----
        out_box = QGroupBox("Output")
        out_layout = QVBoxLayout(out_box)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._log.setFont(mono)
        self._log.setMaximumBlockCount(2000)
        out_layout.addWidget(self._log)
        outer.addWidget(out_box, stretch=1)

        # ----- summary panel -----
        sum_box = QGroupBox("Summary")
        sum_form = QFormLayout(sum_box)
        self._sent_label = QLabel("—")
        self._lost_label = QLabel("—")
        self._loss_label = QLabel("—")
        self._min_label = QLabel("—")
        self._avg_label = QLabel("—")
        self._max_label = QLabel("—")
        for lbl in (self._sent_label, self._lost_label, self._loss_label,
                    self._min_label, self._avg_label, self._max_label):
            lbl.setStyleSheet("font-weight:bold;")
        sum_form.addRow("Packets sent / received:", self._sent_label)
        sum_form.addRow("Packets lost:", self._lost_label)
        sum_form.addRow("Loss:", self._loss_label)
        sum_form.addRow("Min RTT:", self._min_label)
        sum_form.addRow("Avg RTT:", self._avg_label)
        sum_form.addRow("Max RTT:", self._max_label)
        outer.addWidget(sum_box)

        self._worker: _PingWorker | None = None

    # ----- behaviour --------------------------------------------------

    def _default_target(self) -> str:
        """The IP to ping by default — always the *other* PC for this role.

        Server pings the Client; Client pings the Server it sweeps against
        (honouring a Server-host override); Loopback / undecided fall back
        to 127.0.0.1 / the Server IP.
        """
        rs = self.ctx.run_state
        net = self.ctx.config.network
        if rs.role is Role.SERVER:
            return str(net.client_ip)
        if rs.role is Role.LOOPBACK:
            return "127.0.0.1"
        # Client (or undecided): target the Server it would sweep against.
        return rs.server_host_override or str(net.server_ip)

    def refresh(self) -> None:
        """Re-default the target to the other side when the role changed.

        Called on tab activation (the Ping tab isn't rebuilt on a role
        switch). Never clobbers a target the user typed: only updates when
        the field still holds the previously auto-filled value and no ping
        is in flight.
        """
        if self._worker is not None and self._worker.isRunning():
            return
        new_default = self._default_target()
        if (
            new_default != self._auto_target
            and self._target_edit.text().strip() == self._auto_target
        ):
            self._target_edit.setText(new_default)
            self._auto_target = new_default

    @Slot()
    def _on_ping(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        target = self._target_edit.text().strip()
        if not target:
            return

        self._log.clear()
        for lbl in (self._sent_label, self._lost_label, self._loss_label,
                    self._min_label, self._avg_label, self._max_label):
            lbl.setText("…")

        worker = _PingWorker(target, self._count_spin.value())
        worker.line.connect(self._on_line, Qt.ConnectionType.QueuedConnection)
        worker.finished_ok.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        # Round-21 (YY): clear our reference on the *built-in* finished signal
        # (fires after the thread truly exits), not on finished_ok — run()
        # emits finished_ok as its last line while still alive, so nulling
        # there opens a close-window where shutdown() finds nothing to wait
        # on and Qt aborts with "QThread: Destroyed while thread is running".
        worker.finished.connect(self._on_thread_finished)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker

        self._ping_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        worker.start()

    @Slot()
    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self._stop_btn.setEnabled(False)

    def shutdown(self) -> None:
        """Stop the ping worker before teardown so the QThread doesn't
        outlive the app (Qt aborts with 'destroyed while running' otherwise —
        a crash-on-close cause). Called by :meth:`MainWindow.closeEvent`."""
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.request_stop()
                worker.wait(4000)
        except RuntimeError:
            pass

    @Slot(str)
    def _on_line(self, line: str) -> None:
        self._log.appendPlainText(line)

    @Slot()
    def _on_thread_finished(self) -> None:
        # Built-in finished — the thread has actually exited now, so it's safe
        # to drop the reference (see the connect site for why). Sender guard
        # ignores a stale finished from an already-replaced worker. (Round-21 YY.)
        if self.sender() is self._worker:
            self._worker = None

    @Slot(int, str)
    def _on_finished(self, rc: int, full_stdout: str) -> None:
        self._ping_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

        # Parse summary lines.
        m = _PACKETS_RE.search(full_stdout)
        if m:
            sent = int(m.group("sent"))
            rcv = int(m.group("rcv"))
            lost = int(m.group("lost"))
            self._sent_label.setText(f"{sent} sent · {rcv} received")
            self._lost_label.setText(str(lost))
            self._loss_label.setText(f"{m.group('lossp')}%")

        m = _RTT_RE.search(full_stdout)
        if m:
            self._min_label.setText(f"{m.group('min')} ms")
            self._avg_label.setText(f"{m.group('avg')} ms")
            self._max_label.setText(f"{m.group('max')} ms")

        self._log.appendPlainText(f"\n[ping exited with code {rc}]")
