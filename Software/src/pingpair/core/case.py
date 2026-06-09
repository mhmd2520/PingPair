"""Single-case orchestrator.

Spawns the iperf3 server (loopback only — in two-machine mode the remote
laptop runs it), waits a beat, spawns iperf3-client and fping in parallel,
kills fping when iperf3 exits, parses both outputs, and returns a unified
:class:`CaseResult`.

Qt-free: callbacks deliver live lines, blocking ``run`` returns the final
result.  The Script view runs this in a ``QThread``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..config import AppConfig
from .parsers import fping as fping_parser
from .parsers import iperf3 as iperf3_parser
from .plan import TestCase
from .runner import (
    LineCallback,
    ProcRunner,
    RunResult,
    estimate_case_wall_s,
    fping_spec,
    iperf3_client_spec,
    iperf3_server_spec,
)

# Time to wait between starting the iperf3 server and the iperf3 client.
# 0.25 s is generous enough on local hardware that the listener is bound
# before the SYN arrives, but short enough not to be felt by the user.
_SERVER_WARMUP_S = 0.25


@dataclass(slots=True)
class CaseResult:
    """Merged result of one TestCase run."""

    case: TestCase
    iperf_client: iperf3_parser.IperfResult | None
    iperf_intervals: list[iperf3_parser.IperfInterval]
    iperf_server_raw: str
    fping: fping_parser.FpingResult | None
    iperf_client_run: RunResult
    fping_run: RunResult
    iperf_server_run: RunResult | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.iperf_client is not None and self.fping is not None


class CaseRunner:
    """Orchestrates one (payload, bandwidth) test case end-to-end."""

    def __init__(
        self,
        cfg: AppConfig,
        case: TestCase,
        *,
        loopback: bool,
        on_line: LineCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self.case = case
        self.loopback = loopback
        self.on_line = on_line
        self._stop_requested = False
        # Exposed so the Qt wrapper can call .stop() across threads.
        self._iperf_client: ProcRunner | None = None
        self._fping: ProcRunner | None = None
        self._iperf_server: ProcRunner | None = None

    def stop(self) -> None:
        """Best-effort abort. Safe to call from another thread."""
        self._stop_requested = True
        for r in (self._fping, self._iperf_client, self._iperf_server):
            if r is not None:
                r.stop()

    def run(self) -> CaseResult:
        """Blocking run. Returns a CaseResult even on partial failure."""
        # 1) Server (loopback only).  In two-machine mode the remote side starts
        #    its own server via the Phase 3 control channel.
        server_result: RunResult | None = None
        if self.loopback:
            spec = iperf3_server_spec(self.cfg, loopback=True, json=True)
            self._iperf_server = ProcRunner(spec)  # don't pipe to live log
            self._iperf_server.start()
            time.sleep(_SERVER_WARMUP_S)

        # 2) Client + fping in parallel.  fping uses -c <count>, so it exits
        #    on its own once the expected packet count is sent and prints the
        #    min/avg/max summary block we need for the report.
        client_spec = iperf3_client_spec(self.cfg, self.case, loopback=self.loopback)
        ping_spec = fping_spec(self.cfg, self.case, loopback=self.loopback)
        self._iperf_client = ProcRunner(client_spec, on_line=self._line_cb)
        self._fping = ProcRunner(ping_spec, on_line=self._line_cb)

        self._iperf_client.start()
        self._fping.start()

        # 3) Block on the iperf3 client (it's what defines the case duration).
        client_result = self._iperf_client.wait()

        # 4) Wait for fping's natural exit so it can flush its summary block.
        #    On Windows the system timer is ~15.6 ms granular, so ``-p 10`` ends
        #    up delivering one packet every ~16 ms instead of 10 ms.  A 30 s
        #    case at 3000 packets therefore takes closer to 48 s in real time.
        #    The kill-timeout is derived from the same duration-aware model
        #    the ETA uses (``estimate_case_wall_s``) rather than a flat
        #    ``duration_s * 2``: a small ``-p`` interval makes fping run many
        #    times longer than ``duration_s``, and a flat timeout would kill
        #    it before it ever prints the min/avg/max summary line. The 1.5x
        #    headroom + 15 s margin only fire for a genuinely hung fping.
        fping_budget = estimate_case_wall_s(
            self.case.duration_s, self.cfg.fping.interval_ms
        )
        fping_result = self._fping.wait(timeout_s=fping_budget * 1.5 + 15.0)

        # 5) Tear down the local server.  ``-1`` makes it exit on its own as
        #    soon as the client disconnects; we just wait for that to happen
        #    instead of force-terminating, which would leave the listening
        #    socket half-closed and break the next run with
        #    "unable to receive control message — connection reset by peer".
        if self._iperf_server is not None:
            server_result = self._iperf_server.wait(timeout_s=10.0)

        return self._build_result(client_result, fping_result, server_result)

    # ----- helpers -----------------------------------------------------

    def _line_cb(self, source: str, line: str) -> None:
        if self.on_line is not None:
            self.on_line(source, line)

    def _build_result(
        self,
        client: RunResult,
        ping: RunResult,
        server: RunResult | None,
    ) -> CaseResult:
        iperf_metrics: iperf3_parser.IperfResult | None = None
        intervals: list[iperf3_parser.IperfInterval] = []
        fping_metrics: fping_parser.FpingResult | None = None
        error: str | None = None

        # iperf3 text parse for the client run (we asked for text mode).
        if client.returncode == 0:
            try:
                iperf_metrics = iperf3_parser.parse_text(client.stdout)
                intervals = iperf3_parser.parse_intervals(client.stdout)
            except ValueError as exc:
                error = f"iperf3 client output unparseable: {exc}"
        else:
            error = (
                f"iperf3 client failed (rc={client.returncode}): "
                f"{client.stderr.strip() or '(no stderr)'}"
            )

        # fping ≥ 4.0 prints the -s summary block on STDERR, not STDOUT.
        # Combine both streams before parsing so the min/avg/max line is
        # always seen regardless of which side fping decided to use.
        combined = (ping.stdout or "") + "\n" + (ping.stderr or "")
        if combined.strip():
            try:
                fping_metrics = fping_parser.parse(
                    combined, fallback_target=self._target_ip()
                )
            except ValueError:
                # Mid-run abort, or fping never got far enough to print stats.
                if error is None:
                    error = "fping summary not found in captured output."

        return CaseResult(
            case=self.case,
            iperf_client=iperf_metrics,
            iperf_intervals=intervals,
            iperf_server_raw=server.stdout if server is not None else "",
            fping=fping_metrics,
            iperf_client_run=client,
            fping_run=ping,
            iperf_server_run=server,
            error=error,
        )

    def _target_ip(self) -> str:
        from .runner import LOOPBACK_IP
        return LOOPBACK_IP if self.loopback else str(self.cfg.network.server_ip)
