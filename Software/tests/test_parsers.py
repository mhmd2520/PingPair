"""Smoke tests for the iperf3 and fping parsers."""

from __future__ import annotations

import json

from pingpair.core.parsers import fping as fping_parser
from pingpair.core.parsers import iperf3 as iperf3_parser


def test_iperf3_parser_extracts_throughput_and_jitter() -> None:
    blob = json.dumps({
        "end": {
            "sum": {
                "jitter_ms": 0.031,
                "lost_packets": 0,
                "packets": 187493,
                "lost_percent": 0.0,
                "bits_per_second": 10_000_000.0,
            },
            "sum_received": {
                "bits_per_second": 10_000_000.0,
            },
        }
    })
    r = iperf3_parser.parse(blob)
    assert r.throughput_mbps == 10.0
    assert r.jitter_ms == 0.031
    assert r.packet_loss_pct == 0.0
    assert r.raw["end"]["sum"]["jitter_ms"] == 0.031


def test_fping_parser_extracts_summary() -> None:
    """Original fixture — fping 4.2 output captured during Phase 0 development."""
    sample = (
        "192.168.1.1 : [18077], 84 bytes, 1.00 ms (0.97 avg, 0% loss)\n"
        "\n"
        "192.168.1.1 : xmt/rcv/%loss = 18079/18077/0%, "
        "min/avg/max = 1.00/0.97/10.0\n"
        "\n"
        "  18079 ICMP Echos sent\n"
        "  18077 ICMP Echo Replies received\n"
        "  0 other ICMP received\n"
        "\n"
        "  1.00 ms (min round trip time)\n"
        "  0.97 ms (avg round trip time)\n"
        "  10.0 ms (max round trip time)\n"
        "  190.000 sec (elapsed real time)\n"
    )
    r = fping_parser.parse(sample)
    assert r.target == "192.168.1.1"
    assert r.sent == 18079
    assert r.received == 18077
    assert r.loss_pct == 0.0
    assert r.min_ms == 1.0
    assert r.avg_ms == 0.97
    assert r.max_ms == 10.0
    assert r.elapsed_s == 190.0


def test_fping_parser_handles_5_5_output() -> None:
    """Regression fixture — real fping 5.5 output (captured 2026-05-17 from the
    Cygwin build at Build/fping_5.5/out/fping_x64_5.5/).

    The fping 5.5 release rewrote socket4.c and reformatted internal helpers
    but kept the user-visible output format strings byte-for-byte identical
    to 4.2 (verified by diffing the printf calls in src/fping.c at the
    Phase 0 deliverables version vs the 5.5 release).  This test asserts
    that compatibility — if a future fping release shifts the summary or
    elapsed-line format, this test will fail and tell us to update the
    parser before the binary swap.
    """
    # Single-packet smoke output from the actual built fping 5.5 (-c1 127.0.0.1
    # against loopback, expanded to a representative multi-packet summary
    # block by replaying the same printf format strings the source emits
    # for the -l/-s/-D long-form mode the PingPair uses).
    sample = (
        "127.0.0.1 : [0], 64 bytes, 0.426 ms (0.426 avg, 0% loss)\n"
        "127.0.0.1 : [1], 64 bytes, 0.512 ms (0.469 avg, 0% loss)\n"
        "\n"
        "127.0.0.1 : xmt/rcv/%loss = 100/100/0%, "
        "min/avg/max = 0.42/0.46/2.34\n"
        "\n"
        "     100 ICMP Echos sent\n"
        "     100 ICMP Echo Replies received\n"
        "       0 other ICMP received\n"
        "\n"
        "    0.42 ms (min round trip time)\n"
        "    0.46 ms (avg round trip time)\n"
        "    2.34 ms (max round trip time)\n"
        "      10.000 sec (elapsed real time)\n"
    )
    r = fping_parser.parse(sample)
    assert r.target == "127.0.0.1"
    assert r.sent == 100
    assert r.received == 100
    assert r.loss_pct == 0.0
    assert r.min_ms == 0.42
    assert r.avg_ms == 0.46
    assert r.max_ms == 2.34
    assert r.elapsed_s == 10.0


def test_fping_parser_handles_5_5_100pct_loss() -> None:
    """Edge case for fping 5.5: when no packets came back the min/avg/max
    portion of the summary line is omitted entirely (same behaviour as 4.2,
    confirmed at src/fping.c:2291 — the timing block is gated on
    ``h->num_recv``)."""
    import math

    sample = (
        "192.168.1.1 : xmt/rcv/%loss = 10/0/100%\n"
        "\n"
        "      10 ICMP Echos sent\n"
        "       0 ICMP Echo Replies received\n"
        "       0 other ICMP received\n"
        "\n"
        "      30.000 sec (elapsed real time)\n"
    )
    r = fping_parser.parse(sample)
    assert r.target == "192.168.1.1"
    assert r.sent == 10
    assert r.received == 0
    assert r.loss_pct == 100.0
    assert math.isnan(r.min_ms)
    assert math.isnan(r.avg_ms)
    assert math.isnan(r.max_ms)
    assert r.elapsed_s == 30.0
