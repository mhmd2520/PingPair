"""Tests for :mod:`pingpair.analysis.stats`.

Pure-Python aggregator — exercised without spinning up Qt. Covers:

* :func:`run_stats` per-metric min / avg / median / max / stdev
  and sample-count semantics (filtered-out cases must not count;
  ``None`` metric values must not count).
* :func:`stats_for_runs` is the batch wrapper.
* :func:`per_case_diff` rolls up case-by-case deltas, including
  asymmetric coverage (case only in A, case only in B, ``None``
  value on one side, both ``None``).
* The :data:`METRICS` registry has stable codes and the
  ``higher_is_better`` field matches our intuition.
* :func:`fmt` / :func:`fmt_delta` handle ``None`` cleanly and
  use the expected separators.
"""

from __future__ import annotations

from pathlib import Path

from pingpair.analysis import (
    METRICS,
    CasePoint,
    LoadedRun,
    Series,
    fmt,
    fmt_delta,
    metric_by_code,
    per_case_diff,
    run_stats,
    stats_for_runs,
)
from pingpair.analysis.stats import _aggregate_metric


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _case(
    idx: int,
    payload: int,
    bw: int,
    *,
    thr: float | None = 50.0,
    lat: float | None = 2.0,
    loss: float | None = 0.0,
    jit: float | None = 0.5,
    status: str = "ok",
) -> CasePoint:
    """Tiny helper — every field defaulted to a plausible "good" value."""
    return CasePoint(
        case_idx=idx,
        payload_bytes=payload,
        bandwidth_mbps_pushed=bw,
        status=status,
        throughput_mbps_received=thr,
        jitter_ms=jit,
        packet_loss_pct=loss,
        avg_latency_ms=lat,
        min_latency_ms=lat - 0.5 if lat is not None else None,
        max_latency_ms=lat + 0.5 if lat is not None else None,
    )


def _run(label: str, cases: list[CasePoint]) -> LoadedRun:
    """Wrap a list of cases into a single-segment LoadedRun."""
    return LoadedRun(
        path=Path(f"/tmp/{label}.config.json"),
        run_id=label,
        display_label=label,
        schema_version=3,
        started_at=None,
        duration_s=600.0,
        server_ip="192.168.1.1",
        client_ip="192.168.1.2",
        protocol="udp",
        is_multi_segment=False,
        metadata={},
        series=[Series(label=label, cases=cases)],
    )


# ---------------------------------------------------------------------------
# _aggregate_metric — the building block
# ---------------------------------------------------------------------------


def test_aggregate_metric_empty_returns_none_with_zero_samples() -> None:
    s = _aggregate_metric([])
    assert s.samples == 0
    assert s.min is None
    assert s.avg is None
    assert s.median is None
    assert s.max is None
    assert s.stdev is None


def test_aggregate_metric_single_value_has_zero_stdev() -> None:
    s = _aggregate_metric([42.0])
    assert s.samples == 1
    assert s.min == 42.0
    assert s.max == 42.0
    assert s.avg == 42.0
    assert s.median == 42.0
    assert s.stdev == 0.0


def test_aggregate_metric_three_values_min_avg_max() -> None:
    s = _aggregate_metric([10.0, 20.0, 30.0])
    assert s.samples == 3
    assert s.min == 10.0
    assert s.avg == 20.0
    assert s.median == 20.0
    assert s.max == 30.0
    assert s.stdev > 0


# ---------------------------------------------------------------------------
# run_stats
# ---------------------------------------------------------------------------


def test_run_stats_all_cases_passing() -> None:
    run = _run("baseline", [
        _case(1, 200, 10, thr=10.0, lat=1.0, loss=0.0, jit=0.1),
        _case(2, 200, 30, thr=20.0, lat=2.0, loss=0.1, jit=0.2),
        _case(3, 200, 50, thr=30.0, lat=3.0, loss=0.0, jit=0.3),
    ])
    rs = run_stats(run)
    assert rs.run_id == "baseline"
    assert rs.filtered_cases == 3
    assert rs.filtered_ok == 3
    assert rs.by_metric["thr"].avg == 20.0
    assert rs.by_metric["thr"].min == 10.0
    assert rs.by_metric["thr"].max == 30.0
    assert rs.by_metric["lat"].avg == 2.0
    assert rs.by_metric["loss"].max == 0.1
    assert rs.by_metric["jit"].samples == 3


def test_run_stats_filter_skips_excluded_cases() -> None:
    run = _run("filtered", [
        _case(1, 200, 10, thr=10.0),
        _case(2, 600, 30, thr=20.0),
        _case(3, 1000, 50, thr=30.0),
    ])
    # Filter only payload=200 → only case 1.
    rs = run_stats(run, case_filter=lambda c: c.payload_bytes == 200)
    assert rs.filtered_cases == 1
    assert rs.by_metric["thr"].samples == 1
    assert rs.by_metric["thr"].avg == 10.0


def test_run_stats_none_values_skipped_not_zeroed() -> None:
    run = _run("partial", [
        _case(1, 200, 10, thr=10.0, jit=None),
        _case(2, 200, 30, thr=None, jit=0.2),
    ])
    rs = run_stats(run)
    # filtered_cases counts every row regardless of metric availability.
    assert rs.filtered_cases == 2
    # thr has only the case-1 sample.
    assert rs.by_metric["thr"].samples == 1
    assert rs.by_metric["thr"].avg == 10.0
    # jit has only the case-2 sample.
    assert rs.by_metric["jit"].samples == 1
    assert rs.by_metric["jit"].avg == 0.2


def test_run_stats_status_error_still_counted_for_filtered_cases() -> None:
    run = _run("with_errors", [
        _case(1, 200, 10, thr=10.0, status="ok"),
        _case(2, 200, 30, thr=None, status="error"),
    ])
    rs = run_stats(run)
    assert rs.filtered_cases == 2
    assert rs.filtered_ok == 1
    # Failed case had thr=None — not counted in stats.
    assert rs.by_metric["thr"].samples == 1


def test_run_stats_multi_segment_aggregates_across_series() -> None:
    run = _run("multi", [])  # We'll bolt on a second series manually.
    run.series.clear()
    run.is_multi_segment = True
    run.series.append(Series("multi · seg1", [
        _case(1, 200, 10, thr=10.0),
        _case(2, 200, 30, thr=20.0),
    ]))
    run.series.append(Series("multi · seg2", [
        _case(1, 200, 10, thr=30.0),
        _case(2, 200, 30, thr=40.0),
    ]))
    rs = run_stats(run)
    assert rs.filtered_cases == 4
    assert rs.by_metric["thr"].samples == 4
    assert rs.by_metric["thr"].avg == 25.0


def test_stats_for_runs_batches() -> None:
    a = _run("a", [_case(1, 200, 10, thr=10.0)])
    b = _run("b", [_case(1, 200, 10, thr=20.0)])
    out = stats_for_runs([a, b])
    assert [s.run_id for s in out] == ["a", "b"]
    assert out[0].by_metric["thr"].avg == 10.0
    assert out[1].by_metric["thr"].avg == 20.0


# ---------------------------------------------------------------------------
# per_case_diff
# ---------------------------------------------------------------------------


def test_per_case_diff_simple_delta() -> None:
    a = _run("a", [
        _case(1, 200, 10, thr=10.0, lat=1.0, loss=0.0, jit=0.1),
        _case(2, 200, 30, thr=20.0, lat=2.0, loss=0.1, jit=0.2),
    ])
    b = _run("b", [
        _case(1, 200, 10, thr=12.0, lat=0.5, loss=0.0, jit=0.05),
        _case(2, 200, 30, thr=22.0, lat=2.5, loss=0.2, jit=0.3),
    ])
    rows = per_case_diff(a, b)
    assert [r.case_idx for r in rows] == [1, 2]
    assert rows[0].delta["thr"] == 2.0
    assert rows[0].delta["lat"] == -0.5
    assert rows[1].delta["loss"] == 0.1
    # Original values preserved.
    assert rows[0].a_value["thr"] == 10.0
    assert rows[0].b_value["thr"] == 12.0


def test_per_case_diff_case_only_in_one_side_has_none_delta() -> None:
    a = _run("a", [_case(1, 200, 10, thr=10.0)])
    b = _run("b", [
        _case(1, 200, 10, thr=12.0),
        _case(2, 200, 30, thr=20.0),
    ])
    rows = per_case_diff(a, b)
    assert [r.case_idx for r in rows] == [1, 2]
    # Case 2 only in B.
    assert rows[1].a_value["thr"] is None
    assert rows[1].b_value["thr"] == 20.0
    assert rows[1].delta["thr"] is None


def test_per_case_diff_none_metric_one_side() -> None:
    a = _run("a", [_case(1, 200, 10, thr=None)])
    b = _run("b", [_case(1, 200, 10, thr=12.0)])
    rows = per_case_diff(a, b)
    assert rows[0].delta["thr"] is None
    assert rows[0].a_value["thr"] is None
    assert rows[0].b_value["thr"] == 12.0


def test_per_case_diff_filter_applies_to_both_sides() -> None:
    a = _run("a", [
        _case(1, 200, 10, thr=10.0),
        _case(2, 600, 30, thr=20.0),
    ])
    b = _run("b", [
        _case(1, 200, 10, thr=12.0),
        _case(2, 600, 30, thr=22.0),
    ])
    rows = per_case_diff(a, b, case_filter=lambda c: c.payload_bytes == 200)
    assert [r.case_idx for r in rows] == [1]
    assert rows[0].delta["thr"] == 2.0


# ---------------------------------------------------------------------------
# Registry + formatters
# ---------------------------------------------------------------------------


def test_metric_registry_has_four_distinct_codes() -> None:
    codes = [m.code for m in METRICS]
    assert codes == ["thr", "lat", "loss", "jit"]
    assert len(set(codes)) == 4


def test_metric_registry_higher_is_better_intuition() -> None:
    # Throughput up = good.
    assert metric_by_code("thr").higher_is_better is True
    # Latency / loss / jitter up = bad.
    for code in ("lat", "loss", "jit"):
        assert metric_by_code(code).higher_is_better is False


def test_metric_by_code_unknown_raises_keyerror() -> None:
    import pytest as _pt
    with _pt.raises(KeyError):
        metric_by_code("nope")  # type: ignore[arg-type]


def test_fmt_none_returns_em_dash() -> None:
    assert fmt(None) == "—"
    assert fmt(1.2345) == "1.23"
    assert fmt(1.2345, decimals=3) == "1.234"


def test_fmt_delta_uses_explicit_sign() -> None:
    assert fmt_delta(None) == "—"
    assert fmt_delta(1.5) == "+1.50"
    # Unicode minus, not ASCII hyphen — keeps tables aligned.
    assert fmt_delta(-1.5) == "−1.50"
    assert fmt_delta(0.0) == "+0.00"
