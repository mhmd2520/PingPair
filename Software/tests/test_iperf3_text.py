"""Tests for the iperf3 text-mode parser (live, client side)."""

from __future__ import annotations

from pingpair.core.parsers.iperf3 import (
    IperfInterval,
    parse_intervals,
    parse_text,
)


# Realistic iperf3 UDP client output from one of the project screenshots.
# Truncated for clarity but keeps the per-second rows + receiver summary.
_UDP_OUTPUT = """\
Connecting to host 192.168.1.1, port 5201
[  5] local 192.168.1.2 port 61706 connected to 192.168.1.1 port 5201
[ ID] Interval           Transfer     Bitrate         Total Datagrams
[  5]   0.00-1.00   sec  1.19 MBytes  9.99 Mbits/sec  6251
[  5]   1.00-2.00   sec  1.19 MBytes  10.0 Mbits/sec  6249
[  5]   2.00-3.00   sec  1.19 MBytes  10.0 Mbits/sec  6250
[  5]  29.00-30.00  sec  1.19 MBytes  10.0 Mbits/sec  6246
- - - - - - - - - - - - - - - - - - - - - - - - -
[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams
[  5]   0.00-30.00  sec  35.8 MBytes  10.0 Mbits/sec  0.000 ms  4294967296/187494 (0%)  sender
[  5]   0.00-30.00  sec  35.8 MBytes  10.0 Mbits/sec  0.031 ms  0/187493 (0%)  receiver

iperf Done.
"""


_TCP_OUTPUT = """\
Connecting to host 127.0.0.1, port 5201
[  5] local 127.0.0.1 port 65000 connected to 127.0.0.1 port 5201
[ ID] Interval           Transfer     Bitrate
[  5]   0.00-1.00   sec   100 MBytes   839 Mbits/sec
[  5]   1.00-2.00   sec   100 MBytes   840 Mbits/sec
- - - - - - - - - - - - - - - - - - - - - - - - -
[ ID] Interval           Transfer     Bitrate
[  5]   0.00-30.00  sec  3.00 GBytes   840 Mbits/sec                  sender
[  5]   0.00-30.00  sec  3.00 GBytes   839 Mbits/sec                  receiver
"""


def test_text_parser_extracts_udp_metrics() -> None:
    r = parse_text(_UDP_OUTPUT)
    assert r.throughput_mbps == 10.0
    assert r.jitter_ms == 0.031
    assert r.packet_loss_pct == 0.0


def test_text_parser_extracts_tcp_throughput() -> None:
    r = parse_text(_TCP_OUTPUT)
    assert r.throughput_mbps == 839.0
    assert r.jitter_ms == 0.0
    assert r.packet_loss_pct == 0.0


def test_intervals_skip_summary_rows() -> None:
    """The 0.00-30.00 receiver/sender rows must NOT appear as intervals."""
    samples = parse_intervals(_UDP_OUTPUT)
    assert all(s.end_s - s.start_s <= 1.5 for s in samples)
    # The sample data has 4 per-second rows in our truncated text.
    assert len(samples) >= 4
    assert samples[0] == IperfInterval(0.0, 1.0, 9.99)
    assert samples[1].throughput_mbps == 10.0


def test_intervals_handle_streaming_single_line() -> None:
    """The Script view feeds one line at a time; the parser must cope."""
    samples = parse_intervals(
        "[  5]   3.00-4.00   sec  1.19 MBytes  10.0 Mbits/sec  6250"
    )
    assert len(samples) == 1
    assert samples[0].start_s == 3.0
    assert samples[0].end_s == 4.0
    assert samples[0].throughput_mbps == 10.0
