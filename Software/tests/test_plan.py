"""Sanity tests for the test plan generator."""

from __future__ import annotations

from pingpair.config import load_default_config
from pingpair.core.plan import build_plan


def test_default_plan_has_20_cases() -> None:
    cfg = load_default_config()
    plan = build_plan(cfg)
    assert len(plan) == 20, f"expected 20 cases, got {len(plan)}"


def test_plan_indexes_are_contiguous_one_based() -> None:
    cfg = load_default_config()
    plan = build_plan(cfg)
    assert [c.index for c in plan] == list(range(1, 21))


def test_plan_outer_loop_is_payload() -> None:
    """Test Procedure.txt walks payload-major: 200×{10..90}, 600×{10..90}, ..."""
    cfg = load_default_config()
    plan = build_plan(cfg)
    payloads = [c.payload_bytes for c in plan]
    # First 5 are payload 200, next 5 are 600, etc.
    assert payloads[0:5] == [200] * 5
    assert payloads[5:10] == [600] * 5
    assert payloads[10:15] == [1000] * 5
    assert payloads[15:20] == [1300] * 5


def test_every_case_runs_for_30s_by_default() -> None:
    cfg = load_default_config()
    plan = build_plan(cfg)
    assert all(c.duration_s == 30 for c in plan)
