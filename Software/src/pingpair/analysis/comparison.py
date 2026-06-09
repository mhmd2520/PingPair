"""Pure-Python data model for the Analysis-tab Export comparison report (#12).

The Qt layer (the Analysis tab) builds a :class:`ComparisonReport`
from whatever runs are ticked and whatever filter is currently active,
optionally rasters the four charts to PNG, and hands the result to the
``comparison_*`` writers in :mod:`pingpair.reporting`.

Keeping this module Qt-free means the writers can be exercised in
plain pytest without spinning up ``QApplication``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .sidecar_loader import CasePoint, LoadedRun
from .stats import CaseDiff, RunStats, per_case_diff, run_stats


@dataclass(frozen=True, slots=True)
class FilterDescription:
    """Human-readable snapshot of the filter active when the report was built.

    Used purely as a "Filter applied:" line in the report body so the
    reader can tell whether the comparison only covers a subset of cases.
    ``is_default`` is True iff every filter widget is in its default
    ("pass everything") state — the writers hide the line in that case.
    """

    case_lo: int
    case_hi: int
    payloads: tuple[int, ...]
    bandwidths: tuple[int, ...]
    metadata: tuple[tuple[str, str], ...]  # (key, value) pairs (non-empty only)
    is_default: bool

    def lines(self) -> list[str]:
        """Pretty-print as one line per non-default filter row."""
        out: list[str] = []
        if self.case_lo != 1 or self.case_hi != 20:
            out.append(f"Cases: {self.case_lo} to {self.case_hi}")
        if self.payloads:
            out.append(
                "Payloads (B): " + ", ".join(str(p) for p in self.payloads)
            )
        if self.bandwidths:
            out.append(
                "Bandwidths (Mbps): "
                + ", ".join(str(b) for b in self.bandwidths)
            )
        for key, value in self.metadata:
            out.append(f"{key}: {value}")
        return out


@dataclass(slots=True)
class ComparisonReport:
    """Everything a comparison-report writer needs in one bundle.

    ``runs`` are sorted in the order the user ticked them (newest-first
    in the Analysis tab's list). ``per_run_stats`` matches that order.

    ``per_case_diff_rows`` is only populated when exactly two runs are
    included — when len(runs) != 2, the writers skip the diff section.

    ``chart_pngs`` is an optional mapping of metric code ("thr" / "lat"
    / "loss" / "jit") → an on-disk PNG of the overlay chart for that
    metric. The Analysis tab fills this map by rasterising its own
    ``pg.PlotWidget`` instances before calling the writers; non-Qt
    callers (unit tests, headless scripts) leave the map empty and the
    writers omit the figure section instead of crashing.
    """

    title: str
    generated_at: datetime
    runs: list[LoadedRun]
    per_run_stats: list[RunStats]
    filter_description: FilterDescription
    per_case_diff_rows: list[CaseDiff] = field(default_factory=list)
    chart_pngs: dict[str, Path] = field(default_factory=dict)
    notes: str = ""

    @property
    def has_diff_section(self) -> bool:
        return len(self.runs) == 2 and bool(self.per_case_diff_rows)

    @property
    def run_count(self) -> int:
        return len(self.runs)


def build_comparison_report(
    *,
    runs: list[LoadedRun],
    case_filter: Callable[[CasePoint], bool],
    filter_description: FilterDescription,
    title: str = "PingPair — comparison report",
    chart_pngs: dict[str, Path] | None = None,
    notes: str = "",
) -> ComparisonReport:
    """Aggregate run-stats (+ optional pairwise diff) into a single bundle.

    The Qt layer is the typical caller — it passes its filter widget's
    case-predicate as ``case_filter``. Tests pass a no-op lambda.
    """
    stats = [run_stats(r, case_filter=case_filter) for r in runs]
    diff_rows: list[CaseDiff] = []
    if len(runs) == 2:
        # A = older, B = newer (matches the Diff tab). Decide by
        # ``started_at`` rather than caller list order, so a caller that
        # passes the runs oldest-first doesn't silently invert A/B. A
        # run with no timestamp sorts as the oldest.
        older, newer = sorted(
            runs, key=lambda r: r.started_at or datetime.min
        )
        # per_case_diff(run_a, run_b) → a_value=run_a, b_value=run_b,
        # delta=b−a. The writers label column A=older, B=newer, Δ=B−A, so
        # A must be the OLDER run. (Was inverted: passed (newer, older),
        # which put the newer run under "A (older)" and flipped Δ's sign.)
        diff_rows = per_case_diff(older, newer, case_filter=case_filter)
    return ComparisonReport(
        title=title,
        generated_at=datetime.now(),
        runs=runs,
        per_run_stats=stats,
        filter_description=filter_description,
        per_case_diff_rows=diff_rows,
        chart_pngs=dict(chart_pngs or {}),
        notes=notes,
    )
