"""Round 18 (QQ) — duration-aware per-case wall-time estimate.

VM acceptance testing of the Feature 1 ETA showed the old flat
``PER_CASE_OVERHEAD_S = 18`` model badly over-estimated short cases: a
5 s case really finishes in ~9 s, but the model predicted 5 + 18 = 23 s,
so a 20-case sweep estimated at 7m40s actually ran in 2m58s.

``estimate_case_wall_s`` replaces the flat constant with a model that
accounts for fping running at Windows' ~15.6 ms timer granularity (so
the default ``-p 10`` makes fping ~1.56x ``duration_s`` and it, not
iperf3, bounds the case).
"""

from pingpair.core.runner import estimate_case_wall_s


def test_thirty_second_case_matches_two_vm_calibration() -> None:
    # The 2026-05-10 two-VM runs measured 47.6-48.3 s per 30 s case.
    est = estimate_case_wall_s(30, 10)
    assert 47.0 <= est <= 50.0


def test_five_second_case_is_not_over_estimated() -> None:
    # Real ~8.9 s on the VM pair; the old +18 model said 23 s.
    est = estimate_case_wall_s(5, 10)
    assert 8.0 <= est <= 11.0
    assert est < 5 + 18  # strictly better than the old flat model


def test_estimate_grows_monotonically_with_duration() -> None:
    assert (
        estimate_case_wall_s(60, 10)
        > estimate_case_wall_s(30, 10)
        > estimate_case_wall_s(5, 10)
    )


def test_default_ten_ms_interval_stretches_case_by_timer_ratio() -> None:
    # -p 10 against Windows' 15.6 ms tick → fping ~1.56x duration.
    # 100 * 15.6/10 + 1.5 fixed = 157.5
    assert abs(estimate_case_wall_s(100, 10) - 157.5) < 0.01


def test_interval_at_or_above_timer_granularity_runs_realtime() -> None:
    # With -p 20 (> 15.6 ms tick) the timer no longer slows fping, so
    # the case is ~duration + the fixed overhead only.
    est = estimate_case_wall_s(10, 20)
    assert 10.0 < est < 13.0


def test_zero_duration_is_just_fixed_overhead() -> None:
    assert estimate_case_wall_s(0, 10) == 1.5


def test_handles_degenerate_interval_without_crashing() -> None:
    # A zero / negative interval must not divide-by-zero.
    assert estimate_case_wall_s(10, 0) > 0
    assert estimate_case_wall_s(10, -5) > 0
