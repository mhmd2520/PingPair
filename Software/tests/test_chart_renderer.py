"""Smoke tests for the matplotlib chart renderer used by the appendix."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pingpair.analysis import METRICS
from pingpair.analysis.chart_renderer import (
    ascii_sparkline,
    render_breakdown_chart,
    render_metric_chart,
)
from pingpair.analysis.sidecar_loader import (
    CasePoint, LoadedRun, Series,
)


def _case(idx, payload, bw, *, thr=10.0, lat=2.0, loss=0.0, jit=0.5):
    return CasePoint(
        case_idx=idx, payload_bytes=payload, bandwidth_mbps_pushed=bw,
        status="ok", throughput_mbps_received=thr,
        jitter_ms=jit, packet_loss_pct=loss,
        avg_latency_ms=lat, min_latency_ms=lat - 0.5, max_latency_ms=lat + 0.5,
    )


def _run():
    cases = [
        _case(1, 200, 10, thr=10.0),
        _case(2, 200, 30, thr=20.0),
        _case(3, 600, 10, thr=15.0),
        _case(4, 600, 50, thr=25.0),
    ]
    return LoadedRun(
        path=Path("/tmp/x"), run_id="x", display_label="x",
        schema_version=3, started_at=datetime.now(), duration_s=10.0,
        server_ip="1.1.1.1", client_ip="1.1.1.2", protocol="udp",
        is_multi_segment=False, metadata={}, series=[Series("x", cases)],
    )


def test_render_metric_chart_writes_png(tmp_path: Path) -> None:
    out = tmp_path / "t.png"
    result = render_metric_chart(_run(), METRICS[0], out)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 1000  # non-trivial PNG


def test_render_metric_chart_no_data_returns_none(tmp_path: Path) -> None:
    cases = [_case(1, 200, 10, thr=None)]
    run = LoadedRun(
        path=Path("/tmp/x"), run_id="x", display_label="x",
        schema_version=3, started_at=None, duration_s=0.0,
        server_ip="", client_ip="", protocol="udp",
        is_multi_segment=False, metadata={}, series=[Series("x", cases)],
    )
    out = tmp_path / "empty.png"
    assert render_metric_chart(run, METRICS[0], out) is None
    assert not out.exists()


def test_render_breakdown_chart_payload(tmp_path: Path) -> None:
    out = tmp_path / "p.png"
    result = render_breakdown_chart(_run(), METRICS[0], out, by="payload")
    assert result == out
    assert out.exists()


def test_render_breakdown_chart_bandwidth(tmp_path: Path) -> None:
    out = tmp_path / "b.png"
    result = render_breakdown_chart(_run(), METRICS[0], out, by="bandwidth")
    assert result == out
    assert out.exists()


def test_render_breakdown_chart_invalid_axis_raises(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(ValueError, match="unknown breakdown axis"):
        render_breakdown_chart(_run(), METRICS[0], tmp_path / "x.png",
                               by="banana")


def test_ascii_sparkline_basic() -> None:
    s = ascii_sparkline([1.0, 5.0, 9.0])
    assert len(s) == 3
    # The minimum value should be the lowest block, max the highest
    assert s[0] == " " or s[0] == "▁"  # min maps to lowest
    assert s[-1] == "█"  # max maps to highest


def test_ascii_sparkline_with_nones() -> None:
    s = ascii_sparkline([None, 1.0, None, 5.0])
    assert len(s) == 4
    assert s[0] == " "
    assert s[2] == " "


def test_ascii_sparkline_all_none() -> None:
    s = ascii_sparkline([None, None, None])
    assert s == "   "  # three spaces
