"""Pure-Python aggregators for the Analysis tab and downstream writers.

Used by three callers, each of which gets the *same* shape so layout
code can be shared:

* :mod:`pingpair.views._analysis_charts` — the Stats sub-tab.
* :mod:`pingpair.reporting.comparison_*` — the comparison-report
  writers (#12).
* :mod:`pingpair.reporting.*_writer` auto-bundle path (#13) — the
  appendix bolted onto every per-sweep report.

Everything here is pure Python — no Qt, no IO. The Analysis tab
passes pre-loaded :class:`LoadedRun` objects plus its filter
predicates; the writer paths pass freshly-built :class:`LoadedRun`
objects (one for the just-finished sweep) and a pass-through filter.

The four metrics are aliased by short codes (``thr`` / ``lat`` /
``loss`` / ``jit``) so callers don't have to repeat the attribute
names. The bundle dataclass — :class:`RunStats` — carries one
:class:`MetricStats` per code, plus the ok/total counts.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Literal

from .sidecar_loader import CasePoint, LoadedRun


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------


MetricCode = Literal["thr", "lat", "loss", "jit"]


@dataclass(frozen=True, slots=True)
class MetricDef:
    """Static info about one charted metric.

    Carries the user-facing labels (``display`` for tables,
    ``y_label`` for charts) plus the attribute name on
    :class:`CasePoint`. ``higher_is_better`` flips the colour scheme
    in the diff view (#11) so a positive delta on packet loss is
    bad red, but a positive delta on throughput is good green.
    """

    code: MetricCode
    display: str
    y_label: str
    attr: str
    unit: str
    higher_is_better: bool
    # Decimal places this metric is rendered with. Throughput / latency
    # read fine at 2; jitter and loss are small fractional values, so the
    # main report tables show them at 3 — the appendix and comparison
    # tables key off this so every surface agrees on precision.
    decimals: int = 2


METRICS: tuple[MetricDef, ...] = (
    MetricDef("thr",  "Throughput",  "Throughput (Mbps)", "throughput_mbps_received", "Mbps", True,  decimals=2),
    MetricDef("lat",  "Avg latency", "Avg latency (ms)",  "avg_latency_ms",           "ms",   False, decimals=2),
    MetricDef("loss", "Packet loss", "Packet loss (%)",   "packet_loss_pct",          "%",    False, decimals=3),
    MetricDef("jit",  "Jitter",      "Jitter (ms)",       "jitter_ms",                "ms",   False, decimals=3),
)


def metric_by_code(code: MetricCode) -> MetricDef:
    """Look up a :class:`MetricDef` by its short code."""
    for m in METRICS:
        if m.code == code:
            return m
    raise KeyError(code)


# ---------------------------------------------------------------------------
# Stats bundles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricStats:
    """min / avg / median / max / stdev / sample-count for one metric.

    ``samples`` counts only non-None values that passed the filter —
    so a metric with 18 of 20 cases filtered out will report
    ``samples = 2``, and a metric with every value ``None`` reports
    ``samples = 0`` with ``min/avg/max/median/stdev = None``.
    """

    samples: int
    min: float | None
    avg: float | None
    median: float | None
    max: float | None
    stdev: float | None


@dataclass(frozen=True, slots=True)
class RunStats:
    """The aggregator's per-run output.

    ``filtered_cases`` is the total number of CasePoints that passed
    the case-filter (across every series in the run). ``filtered_ok``
    is the subset that also had ``status == "ok"`` — the failure
    rate is ``1 - filtered_ok/filtered_cases`` whenever filtered_cases
    > 0.

    ``by_metric`` is keyed by :class:`MetricCode` (``thr`` / ``lat``
    / ``loss`` / ``jit``) so callers can iterate :data:`METRICS` to
    render a table.
    """

    run_id: str
    display_label: str
    filtered_cases: int
    filtered_ok: int
    by_metric: dict[MetricCode, MetricStats]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_metric(values: list[float]) -> MetricStats:
    """Compute MetricStats from a list of non-None floats (may be empty).

    ``stdev`` is the *population* standard deviation (:func:`pstdev`) —
    the whole sweep is the population of interest, not a sample of a
    larger one. Note this differs from Excel's default ``STDEV`` (sample).
    """
    if not values:
        return MetricStats(0, None, None, None, None, None)
    if len(values) == 1:
        v = values[0]
        return MetricStats(1, v, v, v, v, 0.0)
    return MetricStats(
        samples=len(values),
        min=min(values),
        avg=mean(values),
        median=median(values),
        max=max(values),
        stdev=pstdev(values),
    )


def _iter_filtered_cases(
    run: LoadedRun,
    case_filter: Callable[[CasePoint], bool],
) -> Iterable[CasePoint]:
    """Yield every CasePoint across every series that ``case_filter`` admits."""
    for series in run.series:
        for case in series.cases:
            if case_filter(case):
                yield case


def run_stats(
    run: LoadedRun,
    case_filter: Callable[[CasePoint], bool] | None = None,
) -> RunStats:
    """Compute the per-metric stats bundle for a single run.

    ``case_filter`` defaults to "pass everything"; the Analysis tab
    passes its filter widget predicate so a Stats table refreshes
    when the user narrows the case range, ticks payloads, etc.
    """
    pred = case_filter or (lambda _c: True)

    samples: dict[MetricCode, list[float]] = {m.code: [] for m in METRICS}
    filtered_cases = 0
    filtered_ok = 0
    for case in _iter_filtered_cases(run, pred):
        filtered_cases += 1
        if case.status == "ok":
            filtered_ok += 1
        for metric in METRICS:
            v = getattr(case, metric.attr)
            # Skip None *and* non-finite (NaN/inf) — a single NaN would
            # otherwise poison mean/median/stdev for the whole metric. Newer
            # sidecars never carry NaN (sanitised at the report boundary), but
            # a sidecar written by an older build still could.
            if v is not None and math.isfinite(float(v)):
                samples[metric.code].append(float(v))

    return RunStats(
        run_id=run.run_id,
        display_label=run.display_label,
        filtered_cases=filtered_cases,
        filtered_ok=filtered_ok,
        by_metric={
            m.code: _aggregate_metric(samples[m.code]) for m in METRICS
        },
    )


def stats_for_runs(
    runs: Iterable[LoadedRun],
    case_filter: Callable[[CasePoint], bool] | None = None,
) -> list[RunStats]:
    """Convenience batch wrapper around :func:`run_stats`."""
    return [run_stats(r, case_filter) for r in runs]


# ---------------------------------------------------------------------------
# Per-case diff (powers the Diff sub-tab, task #11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseDiff:
    """One row of the per-case diff table — B minus A for each metric."""

    case_idx: int
    payload_bytes: int
    bandwidth_mbps_pushed: int
    a_value: dict[MetricCode, float | None]
    b_value: dict[MetricCode, float | None]
    delta: dict[MetricCode, float | None]


def _index_by_case(
    run: LoadedRun,
    case_filter: Callable[[CasePoint], bool],
) -> dict[int, CasePoint]:
    """Build {case_idx: CasePoint} for filtered cases.

    Multi-segment runs flatten — if two segments both ran case_idx=3,
    the *last* one wins. The diff view assumes single-segment
    comparisons in practice; the segment overlap edge case is rare
    enough to accept a lossy flatten rather than complicate the API.
    """
    out: dict[int, CasePoint] = {}
    for case in _iter_filtered_cases(run, case_filter):
        out[case.case_idx] = case
    return out


def per_case_diff(
    run_a: LoadedRun,
    run_b: LoadedRun,
    case_filter: Callable[[CasePoint], bool] | None = None,
) -> list[CaseDiff]:
    """Return one :class:`CaseDiff` row per case_idx present in either run.

    ``delta[m] = b - a`` when both sides have a value; ``None``
    otherwise. Rows are sorted by ``case_idx`` so the diff table
    reads left-to-right like the per-case sweep table.
    """
    pred = case_filter or (lambda _c: True)
    a_by = _index_by_case(run_a, pred)
    b_by = _index_by_case(run_b, pred)
    all_idx = sorted(set(a_by) | set(b_by))

    rows: list[CaseDiff] = []
    for idx in all_idx:
        a = a_by.get(idx)
        b = b_by.get(idx)
        a_vals: dict[MetricCode, float | None] = {}
        b_vals: dict[MetricCode, float | None] = {}
        deltas: dict[MetricCode, float | None] = {}
        for metric in METRICS:
            av = getattr(a, metric.attr) if a is not None else None
            bv = getattr(b, metric.attr) if b is not None else None
            a_vals[metric.code] = (
                float(av) if av is not None else None
            )
            b_vals[metric.code] = (
                float(bv) if bv is not None else None
            )
            if av is not None and bv is not None:
                deltas[metric.code] = float(bv) - float(av)
            else:
                deltas[metric.code] = None
        # Prefer payload/bandwidth from whichever side has the row.
        payload = (a or b).payload_bytes if (a or b) is not None else 0
        bw = (a or b).bandwidth_mbps_pushed if (a or b) is not None else 0
        rows.append(
            CaseDiff(
                case_idx=idx,
                payload_bytes=payload,
                bandwidth_mbps_pushed=bw,
                a_value=a_vals,
                b_value=b_vals,
                delta=deltas,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Formatting helpers (re-used by every renderer)
# ---------------------------------------------------------------------------


def fmt(value: float | None, decimals: int = 2) -> str:
    """Format a metric value with the given decimals; ``None``/non-finite → ``"—"``.

    The non-finite guard is the safety net for ``nan``/``inf`` round-tripped
    out of an old sidecar (a pre-fix 100%-loss case serialised ``nan`` as the
    string ``"nan"``, which the loader parses back to ``float('nan')``). Such
    a value must render as the em-dash, never the literal text ``"nan"`` in a
    report cell.
    """
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{decimals}f}"


def fmt_delta(value: float | None, decimals: int = 2) -> str:
    """Like :func:`fmt` but always with a leading sign.

    The sign is taken from the value *after* rounding to ``decimals``,
    so a tiny negative delta (or ``-0.0``) that rounds to zero shows
    ``+0.00`` rather than a misleading ``−0.00``.
    """
    if value is None or not math.isfinite(value):
        return "—"
    rounded = round(value, decimals)
    sign = "−" if rounded < 0 else "+"
    return f"{sign}{abs(rounded):.{decimals}f}"
