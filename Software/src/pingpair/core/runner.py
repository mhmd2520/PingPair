"""Subprocess orchestration — phase 2.

Two layers:

* :class:`ProcRunner` is a Qt-free wrapper around a single ``subprocess.Popen``.
  It streams stdout line-by-line through a callback, captures everything for
  the final result, and supports a clean ``stop()``.  Unit-testable without a
  Qt event loop.

* The argv builders (``iperf3_*_spec``, ``fping_spec``) translate an
  :class:`AppConfig` plus a :class:`TestCase` into a :class:`ProcSpec`. They
  also support the loopback dev mode by overriding both IPs to 127.0.0.1.

The Qt layer (in ``views``) wraps :class:`ProcRunner` in a ``QThread`` and
converts the callbacks into signals.  See :mod:`pingpair.views._qt_runner`.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig
from ..paths import FPING_DIR, FPING_EXE, IPERF3_DIR, IPERF3_EXE
from .plan import TestCase

LOOPBACK_IP = "127.0.0.1"

# Windows: keep the bundled Cygwin binaries from flashing a console window
# every time we spawn one. A sweep spawns iperf3 + fping per case, so in a
# frozen (windowed) GUI build a 20-case run would otherwise pop 40+ black
# console windows. 0 on non-Windows is a harmless no-op for ``creationflags``.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Argv builders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcSpec:
    """Everything needed to spawn one subprocess."""

    name: str               # e.g. 'iperf3-server', 'iperf3-client', 'fping'
    argv: list[str]
    cwd: Path

    @property
    def command_string(self) -> str:
        """Quoted, copy-paste-friendly representation for the Config tab."""
        parts = []
        for a in self.argv:
            parts.append(f'"{a}"' if " " in a else a)
        return " ".join(parts)


def _ips(cfg: AppConfig, loopback: bool) -> tuple[str, str]:
    """Return (server_ip, client_ip), honouring loopback mode."""
    if loopback:
        return LOOPBACK_IP, LOOPBACK_IP
    return str(cfg.network.server_ip), str(cfg.network.client_ip)


def iperf3_server_spec(cfg: AppConfig, *, loopback: bool = False, json: bool = True) -> ProcSpec:
    """Listening side: ``iperf3 -s -i 1 -B <server_ip> [--json] -1``.

    ``-1`` makes the server exit after the single client disconnects, so we
    don't need to track its PID for cleanup between cases.
    """
    server_ip, _ = _ips(cfg, loopback)
    argv = [
        str(IPERF3_EXE),
        "-s",
        "-i", str(cfg.test_plan.interval_s),
        "-B", server_ip,
        "-1",
    ]
    if json:
        argv.append("--json")
    return ProcSpec(name="iperf3-server", argv=argv, cwd=IPERF3_DIR)


def iperf3_client_spec(
    cfg: AppConfig, case: TestCase, *, loopback: bool = False, json: bool = False
) -> ProcSpec:
    """Sending side. Defaults to text output for live charting."""
    server_ip, client_ip = _ips(cfg, loopback)
    argv = [
        str(IPERF3_EXE),
        "-c", server_ip,
    ]
    # -B (bind source) is meaningful in two-machine mode; in loopback both IPs
    # are 127.0.0.1 so we drop -B to avoid iperf3 complaining.
    if not loopback:
        argv += ["-B", client_ip]
    if cfg.test_plan.protocol == "udp":
        argv.append("-u")
    argv += [
        "-i", str(cfg.test_plan.interval_s),
        "-t", str(case.duration_s),
        "-l", str(case.payload_bytes),
        "-b", f"{case.bandwidth_mbps}M",
    ]
    if json:
        argv.append("--json")
    return ProcSpec(name="iperf3-client", argv=argv, cwd=IPERF3_DIR)


def fping_spec(
    cfg: AppConfig,
    case: TestCase | None = None,
    *,
    loopback: bool = False,
) -> ProcSpec:
    """Ping with timestamps + final summary.

    If ``case`` is supplied, we use ``-c <count>`` so fping exits naturally
    once it has sent the expected number of packets, which means it always
    flushes its ``min/avg/max`` summary block (parsed for the report).
    Without a case (e.g. preview rendering in the Config tab) we fall back
    to ``-l`` so the visible CLI string still matches Test Procedure.txt.
    """
    server_ip, client_ip = _ips(cfg, loopback)
    argv = [str(FPING_EXE), server_ip]
    # -S source-address only meaningful when the source isn't the only NIC.
    if not loopback:
        argv += ["-S", client_ip]
    argv += ["-p", str(cfg.fping.interval_ms)]

    extra = list(cfg.fping.extra_args)
    if case is not None:
        # Replace -l (loop forever) with -c <count> so fping self-terminates.
        # count = duration_seconds * 1000 / interval_ms, with a tiny buffer so
        # iperf3 (which decides the case length) is the one that finishes first.
        count = max(1, int(case.duration_s * 1000 / max(cfg.fping.interval_ms, 1)))
        if "-l" in extra:
            extra.remove("-l")
        argv += ["-c", str(count)]
    argv += extra
    return ProcSpec(name="fping", argv=argv, cwd=FPING_DIR)


# ---------------------------------------------------------------------------
# Per-case wall-time estimate
# ---------------------------------------------------------------------------

# Windows' default timer-tick granularity. fping's ``-p`` interval is a
# sleep, and Windows rounds sleeps up to the next ~15.6 ms tick, so a
# configured ``-p 10`` actually delivers a packet roughly every 15.6 ms
# (core/case.py notes the same thing). This is what makes a case take
# ~1.56x ``duration_s`` rather than ``duration_s`` flat.
_WIN_TIMER_GRANULARITY_MS: float = 15.6

# Per-case cost that does NOT scale with duration: the 0.25 s warmup, the
# START_CASE/SERVER_READY/CASE_DONE/SERVER_RESULT control round trips, and
# process spawn/teardown. Measured at ~0.8-1.5 s on the two-VM rig; 1.5 s
# leans slightly toward over-promising the wait.
_CASE_FIXED_OVERHEAD_S: float = 1.5


def estimate_case_wall_s(duration_s: float, fping_interval_ms: float) -> float:
    """Estimate one case's real wall-clock time, in seconds.

    A case runs iperf3 (~``duration_s``) and fping in parallel, then
    blocks until fping finishes. fping sends ``duration_s * 1000 /
    interval_ms`` packets; on Windows each ``-p`` sleep is rounded up to
    the ~15.6 ms timer tick, so for the default ``-p 10`` fping runs
    ~1.56x longer than ``duration_s`` and is the process that actually
    bounds the case.

    Replaces the old flat ``PER_CASE_OVERHEAD_S = 18`` constant, which
    was calibrated only at ``duration_s = 30`` (where 1.56 x 30 ~= 30 + 18
    by coincidence) and badly over-estimated short cases — a 5 s case
    really takes ~9 s, not 5 + 18 = 23 s.
    """
    duration_s = max(0.0, float(duration_s))
    interval = max(0.1, float(fping_interval_ms))
    effective_period_ms = max(interval, _WIN_TIMER_GRANULARITY_MS)
    fping_wall_s = duration_s * effective_period_ms / interval
    return max(duration_s, fping_wall_s) + _CASE_FIXED_OVERHEAD_S


def sweep_time_left_s(
    *,
    position: int,
    total: int,
    case_total_s: float,
    case_elapsed_s: float,
) -> float:
    """Estimate the seconds left in a sweep, from the case in flight.

    ``position`` is the in-flight case's 1-based position and ``total``
    the sweep's case count, so the cases still to run (the in-flight one
    included) are ``total - position + 1``. The estimate is that many
    full per-case budgets (``case_total_s`` — see
    :func:`estimate_case_wall_s`) minus the ``case_elapsed_s`` already
    spent on the current case. Clamped at zero so a slow final case never
    shows a negative countdown.

    Shared by the Run tab's Client and Server panels so both render the
    identical "~M m S s left" readout.
    """
    remaining_cases = max(0, total - position + 1)
    return max(0.0, remaining_cases * case_total_s - case_elapsed_s)


# ---------------------------------------------------------------------------
# ProcRunner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunResult:
    """Final output of a completed subprocess."""

    spec: ProcSpec
    returncode: int
    stdout: str
    stderr: str
    started_at: float
    ended_at: float

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at


LineCallback = Callable[[str, str], None]
"""(source_name, line_without_trailing_newline) -> None"""


class ProcRunner:
    """Qt-free subprocess wrapper with line-streaming.

    Usage::

        runner = ProcRunner(spec, on_line=lambda src, ln: print(src, ln))
        result = runner.run_blocking()
        print(result.returncode, result.duration_s)

    Or non-blocking::

        runner.start()
        ...
        runner.stop()                  # asks process to terminate
        result = runner.wait()         # blocks until exit, returns RunResult

    This class is thread-safe in the sense that ``stop()`` and ``wait()`` may
    be called from a different thread than ``start()``.
    """

    def __init__(
        self,
        spec: ProcSpec,
        on_line: LineCallback | None = None,
    ) -> None:
        self.spec = spec
        self.on_line = on_line
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_buf: list[str] = []
        self._stderr_buf: list[str] = []
        self._reader_threads: list[threading.Thread] = []
        self._started_at = 0.0
        self._ended_at = 0.0
        self._launch_error: str | None = None

    # ----- lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Spawn the subprocess and start the line-reader threads."""
        if self._proc is not None:
            raise RuntimeError("ProcRunner can only be started once.")
        self._started_at = time.monotonic()
        try:
            self._proc = subprocess.Popen(
                self.spec.argv,
                cwd=str(self.spec.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            self._launch_error = str(exc)
            self._ended_at = time.monotonic()
            return

        self._reader_threads = [
            threading.Thread(
                target=self._drain,
                args=(self._proc.stdout, self._stdout_buf),
                daemon=True,
                name=f"{self.spec.name}-stdout",
            ),
            threading.Thread(
                target=self._drain,
                args=(self._proc.stderr, self._stderr_buf),
                daemon=True,
                name=f"{self.spec.name}-stderr",
            ),
        ]
        for t in self._reader_threads:
            t.start()

    def stop(self, timeout_s: float = 3.0) -> None:
        """Politely terminate the process; escalate to kill on timeout."""
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def wait(self, timeout_s: float | None = None) -> RunResult:
        """Block until the process and reader threads have finished.

        If ``timeout_s`` is given and elapses, the process is killed and the
        partial output is returned with returncode = -1.
        """
        if self._launch_error is not None:
            return RunResult(
                spec=self.spec,
                returncode=-1,
                stdout="",
                stderr=self._launch_error,
                started_at=self._started_at,
                ended_at=self._ended_at,
            )
        assert self._proc is not None  # noqa: S101
        try:
            rc = self._proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            rc = self._proc.wait()
        for t in self._reader_threads:
            t.join(timeout=2.0)
        self._ended_at = time.monotonic()
        return RunResult(
            spec=self.spec,
            returncode=rc,
            stdout="".join(self._stdout_buf),
            stderr="".join(self._stderr_buf),
            started_at=self._started_at,
            ended_at=self._ended_at,
        )

    def run_blocking(self) -> RunResult:
        """Convenience: start + wait."""
        self.start()
        return self.wait()

    # ----- internals ---------------------------------------------------

    def _drain(self, stream, buf: list[str]) -> None:
        """Reader-thread body: pump lines from ``stream`` into ``buf`` and the callback."""
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                buf.append(line)
                if self.on_line is not None:
                    try:
                        self.on_line(self.spec.name, line.rstrip("\r\n"))
                    except Exception:  # noqa: BLE001 - never let UI crash kill the reader
                        pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
