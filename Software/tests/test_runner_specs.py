"""Argv-builder tests — make sure the iperf3/fping specs we hand to
subprocess match Test Procedure.txt and the loopback overrides land
where we expect.
"""

from __future__ import annotations

from pingpair.config import load_default_config
from pingpair.core.plan import TestCase
from pingpair.core.runner import (
    LOOPBACK_IP,
    fping_spec,
    iperf3_client_spec,
    iperf3_server_spec,
)


def _case(payload: int = 200, bw: int = 10, t: int = 30) -> TestCase:
    return TestCase(index=1, payload_bytes=payload, bandwidth_mbps=bw, duration_s=t)


def test_iperf3_client_argv_matches_test_procedure_for_udp() -> None:
    cfg = load_default_config()
    spec = iperf3_client_spec(cfg, _case(200, 10), loopback=False)
    assert "-c" in spec.argv
    assert str(cfg.network.server_ip) in spec.argv
    assert "-B" in spec.argv
    assert str(cfg.network.client_ip) in spec.argv
    assert "-u" in spec.argv  # UDP per default config
    assert "-t" in spec.argv and "30" in spec.argv
    assert "-l" in spec.argv and "200" in spec.argv
    assert "-b" in spec.argv and "10M" in spec.argv


def test_iperf3_client_loopback_skips_bind_and_uses_localhost() -> None:
    cfg = load_default_config()
    spec = iperf3_client_spec(cfg, _case(), loopback=True)
    # In loopback, both IPs collapse to 127.0.0.1 and we drop -B to avoid
    # iperf3 complaining about same-host bind.
    assert LOOPBACK_IP in spec.argv
    assert "-B" not in spec.argv


def test_iperf3_server_uses_one_shot_and_json() -> None:
    cfg = load_default_config()
    spec = iperf3_server_spec(cfg, json=True)
    assert "-s" in spec.argv
    assert "-1" in spec.argv      # one-shot, exits after one client
    assert "--json" in spec.argv


def test_fping_argv_matches_test_procedure_when_case_omitted() -> None:
    """Preview mode (case=None): keeps -l so the Generated CLI tab shows the
    canonical command from Test Procedure.txt."""
    cfg = load_default_config()
    spec = fping_spec(cfg, loopback=False)
    assert spec.argv[1] == str(cfg.network.server_ip)
    assert "-S" in spec.argv
    assert str(cfg.network.client_ip) in spec.argv
    assert "-p" in spec.argv and "10" in spec.argv
    for flag in ("-l", "-s", "-D"):
        assert flag in spec.argv, f"missing fping flag {flag}"
    assert "-c" not in spec.argv


def test_fping_with_case_uses_count_not_loop() -> None:
    """Run mode (case provided): substitute -l with -c <count> so fping exits
    on its own and prints the min/avg/max summary block."""
    cfg = load_default_config()
    spec = fping_spec(cfg, _case(t=30), loopback=False)
    assert "-l" not in spec.argv
    assert "-c" in spec.argv
    # 30 s × (1000 / 10 ms) = 3000 packets
    assert "3000" in spec.argv


def test_fping_loopback_drops_source_bind() -> None:
    cfg = load_default_config()
    spec = fping_spec(cfg, loopback=True)
    assert spec.argv[1] == LOOPBACK_IP
    assert "-S" not in spec.argv
