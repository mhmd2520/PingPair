"""Group C-2 — Analysis tab support package.

Loads ``*.json`` sweep sidecars produced by past runs (schema v3-v5)
into a uniform :class:`LoadedRun` shape that the Analysis tab overlays
on the metric charts, and exposes pure-Python aggregators used by the
Stats / Trend / Diff sub-tabs and the comparison-report writers.
"""

from __future__ import annotations

from .sidecar_loader import (
    CasePoint,
    LoadedRun,
    Series,
    SidecarParseError,
    enumerate_sidecars,
    load_many,
    load_sidecar,
)
from .stats import (
    METRICS,
    CaseDiff,
    MetricCode,
    MetricDef,
    MetricStats,
    RunStats,
    fmt,
    fmt_delta,
    metric_by_code,
    per_case_diff,
    run_stats,
    stats_for_runs,
)
from .comparison import (
    ComparisonReport,
    FilterDescription,
    build_comparison_report,
)

__all__ = [
    "CasePoint",
    "LoadedRun",
    "Series",
    "SidecarParseError",
    "enumerate_sidecars",
    "load_many",
    "load_sidecar",
    "METRICS",
    "CaseDiff",
    "MetricCode",
    "MetricDef",
    "MetricStats",
    "RunStats",
    "fmt",
    "fmt_delta",
    "metric_by_code",
    "per_case_diff",
    "run_stats",
    "stats_for_runs",
    "ComparisonReport",
    "FilterDescription",
    "build_comparison_report",
]
