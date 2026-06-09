"""Parse fping output (the long form with ``-D -s -l``).

fping prints one line per echo and a summary block at the end.  We extract
the summary's min/avg/max round-trip time and the xmt/rcv/loss totals.

Example summary block:

    192.168.1.1 : xmt/rcv/%loss = 18079/18077/0%, min/avg/max = 1.00/0.97/10.0

      18079 ICMP Echos sent
      18077 ICMP Echo Replies received
      0 other ICMP received

      1.00 ms (min round trip time)
      0.97 ms (avg round trip time)
      10.0 ms (max round trip time)
      190.000 sec (elapsed real time)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FpingResult:
    target: str
    sent: int
    received: int
    loss_pct: float
    min_ms: float
    avg_ms: float
    max_ms: float
    elapsed_s: float


_SUMMARY_RE = re.compile(
    r"^(?P<target>\S+)\s*:\s*xmt/rcv/%loss\s*=\s*"
    r"(?P<sent>\d+)/(?P<rcv>\d+)/(?P<loss>[\d.]+)%"
    r"(?:,\s*min/avg/max\s*=\s*"
    r"(?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+))?",
    re.MULTILINE,
)
_ELAPSED_RE = re.compile(r"([\d.]+)\s+sec\s*\(elapsed real time\)")


def parse(stdout: str, *, fallback_target: str = "") -> FpingResult:
    """Extract the trailing summary line into a FpingResult."""
    m = _SUMMARY_RE.search(stdout)
    if not m:
        raise ValueError("fping summary line not found in output")

    target = m.group("target") or fallback_target
    sent = int(m.group("sent"))
    rcv = int(m.group("rcv"))
    loss = float(m.group("loss"))

    if m.group("min"):
        min_ms = float(m.group("min"))
        avg_ms = float(m.group("avg"))
        max_ms = float(m.group("max"))
    else:
        # 100% loss case — fping omits the timings.
        min_ms = avg_ms = max_ms = float("nan")

    elapsed_match = _ELAPSED_RE.search(stdout)
    elapsed_s = float(elapsed_match.group(1)) if elapsed_match else 0.0

    return FpingResult(
        target=target,
        sent=sent,
        received=rcv,
        loss_pct=loss,
        min_ms=min_ms,
        avg_ms=avg_ms,
        max_ms=max_ms,
        elapsed_s=elapsed_s,
    )
