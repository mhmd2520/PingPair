"""Test plan generator — turns the (payloads, bandwidths) grid into ordered cases.

This is the canonical mapping between the GUI/JSON config and the cases the
runner executes.  Keeping it isolated lets tests verify ordering and case
count in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig


@dataclass(frozen=True, slots=True)
class TestCase:
    """A single (payload, bandwidth) test case."""

    # Tells pytest not to try to collect this dataclass as a test class.
    __test__ = False

    index: int           # 1-based, used in reports and protocol messages
    payload_bytes: int
    bandwidth_mbps: int
    duration_s: int

    @property
    def label(self) -> str:
        return f"#{self.index:02d} payload={self.payload_bytes}B bw={self.bandwidth_mbps}M"


def build_plan(cfg: AppConfig) -> list[TestCase]:
    """Cartesian product of payloads × bandwidths in the order from Test Procedure.txt.

    Outer loop: payloads.  Inner loop: bandwidths.  This matches the row order
    of Table-1.PNG and the procedure document.
    """
    plan = cfg.test_plan
    cases: list[TestCase] = []
    idx = 1
    for payload in plan.payloads_bytes:
        for bw in plan.bandwidths_mbps:
            cases.append(
                TestCase(
                    index=idx,
                    payload_bytes=payload,
                    bandwidth_mbps=bw,
                    duration_s=plan.duration_s,
                )
            )
            idx += 1
    return cases
