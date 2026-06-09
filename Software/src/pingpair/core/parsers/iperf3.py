"""Parse iperf3 output into a typed result.

Two parsers are exposed:

* :func:`parse` consumes the JSON blob produced by ``iperf3 ... --json``.
  Used for the server-side run that's saved to the config sidecar; the
  schema is documented at https://iperf.fr/iperf-doc.php (search 'JSON').

* :func:`parse_text` consumes the human-readable, line-by-line output
  printed when ``--json`` is *not* used.  We use this for the client-side
  run because ``--json`` buffers all output until the test ends, which
  defeats live charting.  iperf 3.21 does support ``--json-stream`` and
  ``--json-stream-full`` (added in 3.19/3.20), but PingPair still parses
  text intervals — switching to structured streaming would let us drop
  the regex below and is tracked as a future enhancement.

Both parsers return the same :class:`IperfResult` so downstream code is
format-agnostic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IperfResult:
    """Aggregated end-of-test metrics from iperf3."""

    throughput_mbps: float    # received-side bits/s averaged over the whole run
    jitter_ms: float          # UDP only; 0.0 for TCP
    packet_loss_pct: float    # UDP only; 0.0 for TCP
    raw: dict                 # full JSON when available; empty for text mode


# ---------------------------------------------------------------------------
# JSON parser (server side, config sidecar)
# ---------------------------------------------------------------------------


def parse(stdout: str) -> IperfResult:
    """Parse the JSON blob written by ``iperf3 ... --json`` to stdout.

    Raises :class:`ValueError` when iperf3 reports an error or emits no
    ``end`` summary block — mirroring :func:`parse_text`, which raises when
    it can't find a receiver line. A connection failure makes iperf3 emit
    ``{"start": {...}, "error": "unable to connect to server"}`` with **no**
    ``end`` block; without this guard that blob would parse cleanly into an
    all-zeros result and a failed case would masquerade as a real 0 Mbps
    measurement. Both parsers must fail loudly on a non-result so the caller
    records a case *error*, not garbage.
    """
    data = json.loads(stdout)
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(f"iperf3 reported an error: {data['error']}")

    end = data.get("end") if isinstance(data, dict) else None
    if not isinstance(end, dict) or not end:
        raise ValueError("iperf3 JSON has no 'end' summary block.")

    sum_received = end.get("sum_received") or end.get("sum")
    if not isinstance(sum_received, dict) or not sum_received:
        raise ValueError("iperf3 JSON 'end' block has no throughput summary.")
    sum_udp = end.get("sum") or {}

    try:
        throughput_bps = float(sum_received.get("bits_per_second", 0.0))
        jitter_ms = float(sum_udp.get("jitter_ms", 0.0))
        lost_percent = float(sum_udp.get("lost_percent", 0.0))
    except (TypeError, ValueError) as exc:
        # A null / non-numeric metric field — surface as the documented
        # ValueError rather than letting a bare TypeError escape the contract.
        raise ValueError(f"iperf3 JSON has a non-numeric metric: {exc}") from exc

    return IperfResult(
        throughput_mbps=throughput_bps / 1e6,
        jitter_ms=jitter_ms,
        packet_loss_pct=lost_percent,
        raw=data,
    )


# ---------------------------------------------------------------------------
# Text parser (live, client side)
# ---------------------------------------------------------------------------


# Final receiver-side line. Two flavours iperf3 emits:
#
#   UDP, receiver row:
#   [  5]   0.00-30.00  sec  35.8 MBytes  10.0 Mbits/sec  0.031 ms  0/187493 (0%)  receiver
#
#   TCP receiver row:
#   [  5]   0.00-30.00  sec  35.8 MBytes  10.0 Mbits/sec                  receiver
#
# We anchor on "receiver" at end of line so we don't pick up the sender row.
_UDP_RECEIVER_RE = re.compile(
    r"\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<xfer_val>[\d.]+)\s+(?P<xfer_unit>[KMG]?Bytes)\s+"
    r"(?P<rate_val>[\d.]+)\s+(?P<rate_unit>[KMG]?bits/sec)\s+"
    r"(?P<jitter>[\d.]+)\s+ms\s+"
    r"(?P<lost>\d+)/(?P<total>\d+)\s+"
    r"\((?P<pct>[\d.]+)%\)\s+receiver"
)
_TCP_RECEIVER_RE = re.compile(
    r"\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<xfer_val>[\d.]+)\s+(?P<xfer_unit>[KMG]?Bytes)\s+"
    r"(?P<rate_val>[\d.]+)\s+(?P<rate_unit>[KMG]?bits/sec)\s+receiver"
)


# Per-second interval rows (sender side; emitted live during the run).
# UDP example:
#   [  5]   3.00-4.00   sec   1.19 MBytes  10.0 Mbits/sec  6250
# TCP example:
#   [  5]   3.00-4.00   sec   1.19 MBytes  10.0 Mbits/sec
_INTERVAL_RE = re.compile(
    r"\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<xfer_val>[\d.]+)\s+(?P<xfer_unit>[KMG]?Bytes)\s+"
    r"(?P<rate_val>[\d.]+)\s+(?P<rate_unit>[KMG]?bits/sec)"
)


def _to_mbps(value: float, unit: str) -> float:
    """Normalise iperf3's auto-scaled rate units to Mbps."""
    factor = {"bits/sec": 1e-6, "Kbits/sec": 1e-3, "Mbits/sec": 1.0, "Gbits/sec": 1e3}
    return value * factor.get(unit, 1.0)


@dataclass(frozen=True, slots=True)
class IperfInterval:
    """One per-second sample emitted by iperf3 during a run."""

    start_s: float
    end_s: float
    throughput_mbps: float


def parse_intervals(stdout: str) -> list[IperfInterval]:
    """Pull out every per-second interval row from iperf3 text output.

    The receiver/sender summary lines also match :data:`_INTERVAL_RE`, but they
    span the full duration (e.g. 0.00-30.00) and their start/end indicate the
    final summary; we filter those out by requiring ``end - start <= 1.5`` so
    only per-second rows survive.
    """
    samples: list[IperfInterval] = []
    for m in _INTERVAL_RE.finditer(stdout):
        start = float(m.group("start"))
        end = float(m.group("end"))
        if end - start > 1.5:
            continue
        rate = _to_mbps(float(m.group("rate_val")), m.group("rate_unit"))
        samples.append(IperfInterval(start, end, rate))
    return samples


def parse_text(stdout: str) -> IperfResult:
    """Parse iperf3's plain-text client output into an IperfResult."""
    udp = _UDP_RECEIVER_RE.search(stdout)
    if udp:
        throughput = _to_mbps(float(udp.group("rate_val")), udp.group("rate_unit"))
        return IperfResult(
            throughput_mbps=throughput,
            jitter_ms=float(udp.group("jitter")),
            packet_loss_pct=float(udp.group("pct")),
            raw={},
        )
    tcp = _TCP_RECEIVER_RE.search(stdout)
    if tcp:
        throughput = _to_mbps(float(tcp.group("rate_val")), tcp.group("rate_unit"))
        return IperfResult(
            throughput_mbps=throughput, jitter_ms=0.0,
            packet_loss_pct=0.0, raw={},
        )
    raise ValueError(
        "Could not find an iperf3 'receiver' summary line in the output."
    )
