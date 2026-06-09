"""Qt-thread wrappers around the headless core/ runners.

Keeps Qt out of ``core/`` while letting the Script view receive live
streaming lines and final results via signals.

Two workers:

* :class:`ServerWorker` — :class:`pingpair.core.control.server.ControlServer`
  on a background thread; emits per-event signals so the GUI can show
  "client connected" / "case 7/20 in progress".
* :class:`SweepWorker` — :class:`pingpair.core.control.client.ControlClient`
  driving a full 20-case sweep against a remote Server; emits per-case
  progress and a final :class:`SweepResult`.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

from ..config import AppConfig
from ..core.control.client import ControlClient, SweepResult
from ..core.control.server import ControlServer


# ---------------------------------------------------------------------------
# Report save (off the GUI thread)
# ---------------------------------------------------------------------------


class ReportSaveWorker(QThread):
    """Writes a sweep's report set off the GUI thread.

    Building + saving a report (docx / xlsx / pdf / txt + the matplotlib chart
    renders) takes a couple of seconds; doing it inline froze the window right
    after the user clicked **Save** on the finish popup. This runs the supplied
    ``save_fn`` (a no-arg closure over ``report_view._save_sweep`` /
    ``_save_multi_sweep``) on a worker thread and reports the outcome via
    ``done(written_paths, error_message)`` — ``error_message`` is empty on
    success. The work is pure file I/O + Agg rendering with no Qt access, so
    it's safe here; the result crosses back to the GUI thread on the queued
    signal.
    """

    done = Signal(object, str)  # (list[Path] written, error message)

    def __init__(
        self,
        save_fn: Callable[[], list],
        *,
        logger=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._save_fn = save_fn
        self._logger = logger

    def run(self) -> None:  # noqa: D401
        try:
            written = self._save_fn()
        except Exception as exc:  # noqa: BLE001
            if self._logger is not None:
                # Full traceback to the log (thread-safe); short text to the UI.
                self._logger.exception("report save failed in worker: %s", exc)
            self.done.emit([], str(exc))
            return
        self.done.emit(list(written), "")


def run_save_in_background(
    parent,
    save_fn: Callable[[], list],
    *,
    logger=None,
    title: str = "Saving report…",
) -> tuple[list, str]:
    """Run a report-writing ``save_fn`` off the GUI thread behind a small
    modal "Saving…" indicator. Returns ``(written_paths, error_message)`` —
    ``error_message`` is empty on success.

    Building a multi-format report set (docx / xlsx / pdf / txt + matplotlib
    chart renders) takes a couple of seconds; doing it inline freezes the
    window. A :class:`ReportSaveWorker` does the write on a worker thread
    while a local :class:`QEventLoop` keeps the UI painting. The call still
    blocks until the save finishes, so each caller's straight-line flow
    (status text, finish popup, progress reset) is unchanged — only the
    freeze is gone.

    Shared by the post-sweep auto-save (Run tab), the Save Options tab's
    manual save, and the Analysis tab's comparison export. ``save_fn`` must
    be Qt-free (pure file I/O / Agg rendering); rasterise any chart widgets
    on the GUI thread *before* calling this.
    """
    from PySide6.QtCore import QEventLoop, Qt
    from PySide6.QtWidgets import QProgressDialog

    progress = QProgressDialog(title, "", 0, 0, parent)
    progress.setWindowTitle("Saving report")
    progress.setCancelButton(None)  # a half-written report is worse than waiting
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

    loop = QEventLoop()
    outcome: dict[str, object] = {"written": [], "err": ""}
    worker = ReportSaveWorker(save_fn, logger=logger, parent=parent)

    def _on_done(written: object, err: str) -> None:
        outcome["written"] = written
        outcome["err"] = err

    worker.done.connect(_on_done)
    # Quit the local loop only once the thread has truly finished, so the
    # QThread is safe to deleteLater (the project's QThread-teardown rule of
    # acting on the built-in `finished` signal).
    worker.finished.connect(loop.quit)
    worker.finished.connect(worker.deleteLater)
    worker.start()
    progress.show()
    loop.exec()  # pump events: UI repaints + busy bar animates until done
    progress.close()
    return list(outcome["written"]), str(outcome["err"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------


class ServerWorker(QThread):
    """Runs ControlServer.serve_forever on a background thread.

    The ``event`` signal fires on every state transition the underlying
    server emits.  Use it to update the UI's listening status, current
    case, error notices etc.
    """

    event = Signal(str, dict)   # (event_name, data)

    def __init__(self, cfg: AppConfig, *, bind_host: str | None = None) -> None:
        super().__init__()
        self._cfg = cfg
        self._bind_host = bind_host
        self._server: ControlServer | None = None

    def run(self) -> None:  # noqa: D401
        # Round-6 (Task Y, 2026-05-13): wrap the inner blocking call so
        # any unexpected exception in ControlServer surfaces as an
        # ``error`` event instead of taking down the QThread silently.
        # Without this, a bug deep inside server.py would just print
        # "Error calling Python override of QThread::run()" to stderr
        # and leave the GUI with no idea what happened.
        self._server = ControlServer(
            self._cfg,
            on_event=self._forward,
        )
        try:
            self._server.serve_forever(bind_host=self._bind_host)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            self._forward(
                "error",
                {"message": f"server thread crashed: {exc}", "traceback": tb},
            )

    def request_stop(self) -> None:
        if self._server is not None:
            self._server.stop()

    # ------------------------------------------------------------------

    def _forward(self, name: str, data: dict) -> None:
        self.event.emit(name, data)


# ---------------------------------------------------------------------------
# Client side (full sweep)
# ---------------------------------------------------------------------------


class SweepWorker(QThread):
    """Drives a full 20-case sweep against a remote Server."""

    event = Signal(str, dict)            # control-protocol events
    line_received = Signal(str, str)     # live stdout from iperf3/fping
    sweep_finished = Signal(object)      # SweepResult

    def __init__(
        self,
        cfg: AppConfig,
        *,
        server_host: str | None = None,
        selected_indexes: list[int] | None = None,
        loopback: bool = False,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._server_host = server_host
        self._selected_indexes = selected_indexes
        self._loopback = loopback
        self._client: ControlClient | None = None

    def run(self) -> None:  # noqa: D401
        # Round-6 (Task Y, 2026-05-13): wrap the run_sweep call so the
        # GUI is never stuck waiting for ``sweep_finished``. Before
        # this, an unhandled OSError would propagate out of run_sweep,
        # the QThread would print "Error calling Python override of
        # QThread::run()" to stderr, and the UI sat at "Running"
        # forever because the terminal signal never fired.
        self._client = ControlClient(
            self._cfg,
            on_event=self._forward_event,
            on_line=self._forward_line,
        )
        try:
            result: SweepResult = self._client.run_sweep(
                server_host=self._server_host,
                selected_indexes=self._selected_indexes,
                loopback=self._loopback,
            )
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            self._forward_event(
                "error",
                {"message": f"sweep thread crashed: {exc}", "traceback": tb},
            )
            result = SweepResult(started_at=time.time(), ended_at=time.time())
        self.sweep_finished.emit(result)

    def request_stop(self) -> None:
        if self._client is not None:
            self._client.stop()

    # ------------------------------------------------------------------

    def _forward_event(self, name: str, data: dict) -> None:
        self.event.emit(name, data)

    def _forward_line(self, source: str, line: str) -> None:
        self.line_received.emit(source, line)
