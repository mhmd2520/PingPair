"""Regression guards for the /final production-readiness review fixes.

Each test pins one bug found by the multi-agent review so it can't silently
return. Grouped by the fix letter used in the review write-up:

* A  — NaN latency (100%-loss case) must collapse to None, not leak `nan`.
* B  — AnalysisFilters must hand the comparison writers a FilterDescription
       OBJECT (it used to return a str → AttributeError → broken export).
* C  — iperf3 JSON parse() must RAISE on an error/empty blob, not return zeros.
* G  — a hand-edited sidecar with a string schema_version/duration_s must not
       abort the whole folder scan.
* H  — the updater default opener must refuse a non-HTTPS URL / redirect.
* I  — a garbage NIC override must fall back to the validated profile value.
* J  — a traversal/absolute filename pattern must not escape the Reports dir.
* K  — winexec.harden_argv leaves non-system tools alone, resolves known ones.
* L  — the update swap script refuses quote-bearing paths and pins PATH.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fix C — iperf3 JSON parse() fails loudly on a non-result
# ---------------------------------------------------------------------------


def test_iperf3_parse_raises_on_error_blob() -> None:
    from pingpair.core.parsers.iperf3 import parse

    blob = json.dumps({"start": {}, "error": "unable to connect to server"})
    with pytest.raises(ValueError, match="error"):
        parse(blob)


def test_iperf3_parse_raises_on_missing_end_block() -> None:
    from pingpair.core.parsers.iperf3 import parse

    with pytest.raises(ValueError, match="end"):
        parse(json.dumps({"start": {"connected": []}}))


def test_iperf3_parse_still_reads_a_valid_blob() -> None:
    from pingpair.core.parsers.iperf3 import parse

    good = json.dumps(
        {"end": {"sum_received": {"bits_per_second": 10_000_000.0},
                 "sum": {"jitter_ms": 0.5, "lost_percent": 1.0}}}
    )
    res = parse(good)
    assert res.throughput_mbps == pytest.approx(10.0)
    assert res.jitter_ms == pytest.approx(0.5)
    assert res.packet_loss_pct == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Fix A — NaN latency collapses to None at the report boundary + in stats
# ---------------------------------------------------------------------------


def test_finite_or_none_collapses_nan_and_inf() -> None:
    from pingpair.reporting.run_report import _finite_or_none

    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(float("inf")) is None
    assert _finite_or_none(None) is None
    assert _finite_or_none(1.5) == 1.5
    assert _finite_or_none(0.0) == 0.0


def test_run_stats_skips_nan_latency() -> None:
    """A single NaN latency must not poison mean/median/stdev for the run."""
    from pingpair.analysis import CasePoint, LoadedRun, Series, run_stats

    cases = [
        CasePoint(
            case_idx=1, payload_bytes=200, bandwidth_mbps_pushed=10,
            status="ok", throughput_mbps_received=10.0, jitter_ms=0.5,
            packet_loss_pct=0.0, avg_latency_ms=2.0,
            min_latency_ms=1.5, max_latency_ms=2.5,
        ),
        CasePoint(  # a 100%-loss case: latencies NaN
            case_idx=2, payload_bytes=200, bandwidth_mbps_pushed=30,
            status="ok", throughput_mbps_received=10.0, jitter_ms=0.5,
            packet_loss_pct=100.0, avg_latency_ms=float("nan"),
            min_latency_ms=float("nan"), max_latency_ms=float("nan"),
        ),
    ]
    run = LoadedRun(
        path=Path("/tmp/x.json"), run_id="x", display_label="x",
        schema_version=5, started_at=None, duration_s=60.0,
        server_ip="192.168.1.1", client_ip="192.168.1.2", protocol="udp",
        is_multi_segment=False, metadata={},
        series=[Series(label="x", cases=cases)],
    )
    lat = run_stats(run).by_metric["lat"]
    assert lat.samples == 1  # only the finite case counted
    assert lat.avg == pytest.approx(2.0)
    assert lat.avg == lat.avg  # not NaN (NaN != NaN)


# ---------------------------------------------------------------------------
# Fix B — AnalysisFilters.filter_description() returns a FilterDescription
# ---------------------------------------------------------------------------


def test_analysis_filters_returns_filter_description_object(qapp, tmp_path) -> None:
    from pingpair.analysis import (
        CasePoint,
        FilterDescription,
        LoadedRun,
        Series,
        build_comparison_report,
    )
    from pingpair.reporting import save_comparison_report
    from pingpair.views._analysis_filters import AnalysisFilters

    filters = AnalysisFilters()
    desc = filters.filter_description()
    assert isinstance(desc, FilterDescription)
    assert desc.is_default is True  # nothing typed → default

    filters._payload_edit.setText("200, 600")
    desc2 = filters.filter_description()
    assert desc2.is_default is False
    assert desc2.payloads == (200, 600)

    # End-to-end: the writers must accept it (they call .is_default / .lines()).
    run = LoadedRun(
        path=Path("/tmp/r.json"), run_id="r", display_label="r",
        schema_version=5, started_at=datetime(2026, 6, 2), duration_s=60.0,
        server_ip="192.168.1.1", client_ip="192.168.1.2", protocol="udp",
        is_multi_segment=False, metadata={},
        series=[Series(label="r", cases=[CasePoint(
            case_idx=1, payload_bytes=200, bandwidth_mbps_pushed=10,
            status="ok", throughput_mbps_received=10.0, jitter_ms=0.5,
            packet_loss_pct=0.0, avg_latency_ms=2.0,
            min_latency_ms=1.5, max_latency_ms=2.5)])],
    )
    report = build_comparison_report(
        runs=[run], case_filter=filters.case_passes,
        filter_description=filters.filter_description(),
    )
    written = save_comparison_report(report, tmp_path, "cmp", ["txt"])
    assert written and written[0].exists()


# ---------------------------------------------------------------------------
# Fix G — tolerant top-level sidecar scalar coercion
# ---------------------------------------------------------------------------


def test_load_sidecar_tolerates_bad_top_level_scalars(tmp_path) -> None:
    from pingpair.analysis import load_sidecar

    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({
            "schema_version": "not-an-int",
            "run_id": "x",
            "duration_s": "oops",
            "cases": [],
        }),
        encoding="utf-8",
    )
    run = load_sidecar(path)  # must NOT raise
    assert run.schema_version == 1   # default
    assert run.duration_s == 0.0     # default


# ---------------------------------------------------------------------------
# Fix H — strict-HTTPS updater opener
# ---------------------------------------------------------------------------


def test_default_open_rejects_non_https() -> None:
    from pingpair.core.updater import UpdateCheckError, _default_open

    with pytest.raises(UpdateCheckError, match="non-HTTPS"):
        _default_open("http://example.com/x.zip", timeout=1)


def test_https_redirect_handler_blocks_downgrade() -> None:
    from pingpair.core.updater import _HTTPSOnlyRedirectHandler

    handler = _HTTPSOnlyRedirectHandler()
    req = urllib.request.Request("https://a/x")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            req, io.BytesIO(b""), 302, "Found", {}, "http://evil/x"
        )


# ---------------------------------------------------------------------------
# Fix I — NIC override validated as IPv4 before reaching netsh
# ---------------------------------------------------------------------------


def test_garbage_nic_override_falls_back_to_profile() -> None:
    from pingpair.config import load_default_config
    from pingpair.context import NicOverride, Role
    from pingpair.core.nic_resolve import effective_nic_for_role

    cfg = load_default_config()
    override = NicOverride(
        use_custom=True, ip="not-an-ip; rm -rf", subnet="garbage", gateway="x"
    )
    eff = effective_nic_for_role(Role.SERVER, cfg, override)
    assert eff.ip == str(cfg.network.server_ip)        # profile, not garbage
    assert eff.subnet_mask == str(cfg.network.subnet_mask)
    assert eff.source == "profile"                     # nothing valid contributed


def test_valid_nic_override_is_used() -> None:
    from pingpair.config import load_default_config
    from pingpair.context import NicOverride, Role
    from pingpair.core.nic_resolve import effective_nic_for_role

    cfg = load_default_config()
    override = NicOverride(use_custom=True, ip="10.0.0.5", subnet="", gateway=None)
    eff = effective_nic_for_role(Role.SERVER, cfg, override)
    assert eff.ip == "10.0.0.5"
    assert eff.source == "override"


# ---------------------------------------------------------------------------
# Fix J — report filename pattern can't escape the Reports dir
# ---------------------------------------------------------------------------


def test_render_filename_sanitizes_traversal() -> None:
    from pingpair.reporting.run_report import render_filename

    started = datetime(2026, 6, 2, 9, 47)
    for pattern in (r"..\..\Windows\Temp\x", "../../etc/passwd", r"C:\evil"):
        out = render_filename(pattern, started)
        assert "/" not in out and "\\" not in out
        assert ".." not in out
        assert ":" not in out

    # A normal pattern is untouched (token-expanded).
    assert render_filename("PingPair_{date}_{time}", started) == "PingPair_2026-06-02_0947"


# ---------------------------------------------------------------------------
# Fix K — System32 hardening helper
# ---------------------------------------------------------------------------


def test_harden_argv_leaves_non_system_tool_alone() -> None:
    from pingpair.core.winexec import harden_argv, system_tool

    # iperf3 is bundled, not a System32 tool — must pass through unchanged.
    assert system_tool("iperf3") == "iperf3"
    assert harden_argv([]) == []
    out = harden_argv(["netsh", "interface", "show"])
    assert out[1:] == ["interface", "show"]      # args untouched
    assert out[0].lower().endswith("netsh") or out[0].lower().endswith("netsh.exe")


# ---------------------------------------------------------------------------
# Fix L — update swap script refuses unsafe paths + pins PATH
# ---------------------------------------------------------------------------


def test_build_swap_script_refuses_quote_in_path() -> None:
    from pingpair.core.update_apply import UpdateApplyError, build_swap_script

    with pytest.raises(UpdateApplyError):
        build_swap_script(123, Path('C:\\a"evil'), Path("C:\\install"))


def test_build_swap_script_pins_system32_path() -> None:
    from pingpair.core.update_apply import build_swap_script

    script = build_swap_script(123, Path("C:\\staging"), Path("C:\\install"))
    assert "System32" in script
    assert 'set "PATH=' in script
