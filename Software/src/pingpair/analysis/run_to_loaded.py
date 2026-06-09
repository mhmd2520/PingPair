"""Convert a freshly-built :class:`RunReport` (or :class:`MultiSegmentRunReport`)
into the :class:`LoadedRun` shape consumed by the analysis aggregators.

Lets the per-sweep auto-bundle appendix (#13) reuse the same
``run_stats``/``per_case_diff`` machinery the Analysis tab uses,
without having to round-trip through a ``.json`` sidecar on disk first.
"""

from __future__ import annotations

from pathlib import Path

from .sidecar_loader import CasePoint, LoadedRun, Series


def _to_case_point(cm) -> CasePoint:
    """Lift one :class:`reporting.run_report.CaseMetrics` to a CasePoint."""
    return CasePoint(
        case_idx=cm.case_idx,
        payload_bytes=cm.payload_bytes,
        bandwidth_mbps_pushed=cm.bandwidth_mbps_pushed,
        status=cm.status,
        throughput_mbps_received=cm.throughput_mbps_received,
        jitter_ms=cm.jitter_ms,
        packet_loss_pct=cm.packet_loss_pct,
        avg_latency_ms=cm.avg_latency_ms,
        min_latency_ms=cm.min_latency_ms,
        max_latency_ms=cm.max_latency_ms,
    )


def from_run_report(report) -> LoadedRun:
    """Convert a RunReport (single sweep) to a single-segment LoadedRun.

    Imported lazily by the appendix code so the analysis package
    doesn't have a hard dep on the reporting package.
    """
    cases = [_to_case_point(c) for c in report.cases]
    metadata = dict(getattr(report, "metadata", {}) or {})
    return LoadedRun(
        path=Path("(in-memory)"),
        run_id=report.run_id,
        display_label=report.run_id,
        schema_version=3,
        started_at=report.started_at,
        duration_s=report.duration_s,
        server_ip=report.server_ip,
        client_ip=report.client_ip,
        protocol=report.protocol,
        is_multi_segment=False,
        metadata=metadata,
        series=[Series(label=report.run_id, cases=cases)],
    )


def from_multi_segment_report(report) -> LoadedRun:
    """Convert a MultiSegmentRunReport to a multi-segment LoadedRun."""
    series: list[Series] = []
    for seg in report.segments:
        cases = [_to_case_point(c) for c in seg.cases]
        label = f"{report.run_id} · {seg.label}"
        series.append(Series(label=label, cases=cases))
    metadata = dict(getattr(report, "metadata", {}) or {})
    return LoadedRun(
        path=Path("(in-memory)"),
        run_id=report.run_id,
        display_label=report.run_id,
        schema_version=4,
        started_at=report.started_at,
        duration_s=report.duration_s,
        server_ip=report.server_ip,
        client_ip=report.client_ip,
        protocol=report.protocol,
        is_multi_segment=True,
        metadata=metadata,
        series=series,
    )
