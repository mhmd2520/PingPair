"""Write the matched ``<basename>.json`` sidecar.

The sidecar is the report's machine-readable counterpart: full
provenance for the run, including every CLI string, return code, and
the raw iperf3 server-side JSON for each case. It's the input to a
future "Replay this run" feature, and the audit trail for any test
record produced from a PingPair sweep.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .run_report import MultiSegmentRunReport, RunReport


def _serialise(report: RunReport) -> dict:
    """Convert RunReport to a JSON-friendly dict.

    Schema versions:
      1 - Phase 4 baseline.
      2 - Slice 1: added top-level ``metadata`` block (test-record fields).
      3 - Group B: added ``selected_case_indexes`` recording the subset
          the user requested before the sweep ran. Empty list = full
          20-case sweep (back-compat with v2 readers).
    """
    data = {
        # Group F (Q1, 2026-05-16): schema bumped 3 -> 5 to record the
        # profile-level ``gateway`` (None for canonical point-to-point)
        # plus the per-PC ``nic_override`` snapshot at sweep time
        # (None when the user hadn't applied a custom config). Both
        # fields are additive — v3/v4 readers parse v5 cleanly, they
        # just won't see the new keys.
        "schema_version": 5,
        "run_id": report.run_id,
        "started_at": report.started_at.isoformat(),
        "ended_at": report.ended_at.isoformat(),
        "duration_s": report.duration_s,
        "server_ip": report.server_ip,
        "client_ip": report.client_ip,
        "gateway": report.gateway,
        "nic_override": report.nic_override,
        "protocol": report.protocol,
        "fping_version": report.fping_version,
        "iperf3_version": report.iperf3_version,
        "app_version": report.app_version,
        "cases_ok": report.cases_ok,
        "cases_total": report.cases_total,
        # Test-record metadata. Always present (even if all values are
        # empty strings) so consumers don't have to handle two schema
        # variants.
        "metadata": dict(report.metadata or {}),
        # Group B: subset the user requested. Empty = full 20-case sweep.
        "selected_case_indexes": list(report.selected_case_indexes or []),
        # Optional cable length under test (metres, as typed); "" = unset.
        # Additive field — older readers ignore it.
        "cable_length_m": report.cable_length_m or "",
        "cfg_snapshot": report.cfg_snapshot,
        "cases": [asdict(c) for c in report.cases],
    }
    return data


def write_config(report: RunReport, dest: Path) -> None:
    """Dump the report as ``<basename>.json`` next to the report."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as f:
        json.dump(_serialise(report), f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Group C-1: multi-segment sidecar (schema v5)
# ---------------------------------------------------------------------------


def _serialise_multi(report: MultiSegmentRunReport) -> dict:
    """Convert MultiSegmentRunReport to a JSON-friendly dict.

    Schema versions:
      1 — Phase 4 baseline.
      2 — Slice 1: added top-level ``metadata`` block.
      3 — Group B: added ``selected_case_indexes``.
      4 — Group C-1: multi-segment runs. Top-level ``segments: [...]``
          replaces the v3 flat ``cases`` list; each segment carries its
          own per-case list plus label / status / timing.
          ``selected_case_indexes`` is shared across all segments (the
          subset is locked at the start of the multi-segment run).
    """
    return {
        # Group F (Q1, 2026-05-16): schema bumped 4 -> 5 with the same
        # additive ``gateway`` + ``nic_override`` snapshot as the single-
        # segment v5 sidecar. The ``segments`` block introduced in v4
        # is unchanged.
        "schema_version": 5,
        "run_id": report.run_id,
        "started_at": report.started_at.isoformat(),
        "ended_at": report.ended_at.isoformat(),
        "duration_s": report.duration_s,
        "server_ip": report.server_ip,
        "client_ip": report.client_ip,
        "gateway": report.gateway,
        "nic_override": report.nic_override,
        "protocol": report.protocol,
        "fping_version": report.fping_version,
        "iperf3_version": report.iperf3_version,
        "app_version": report.app_version,
        "segments_ok": report.segments_ok,
        "segments_total": report.segments_total,
        "total_cases_ok": report.total_cases_ok,
        "total_cases": report.total_cases,
        "metadata": dict(report.metadata or {}),
        "selected_case_indexes": list(report.selected_case_indexes or []),
        # Optional cable length under test (metres, as typed); "" = unset.
        "cable_length_m": report.cable_length_m or "",
        "cfg_snapshot": report.cfg_snapshot,
        "segments": [
            {
                "segment_idx": s.segment_idx,
                "label": s.label,
                "started_at": s.started_at.isoformat(),
                "ended_at": s.ended_at.isoformat(),
                "duration_s": s.duration_s,
                "status": s.status,
                "error": s.error,
                "cases_ok": s.cases_ok,
                "cases_total": s.cases_total,
                "cases": [asdict(c) for c in s.cases],
            }
            for s in report.segments
        ],
    }


def write_multi_config(report: MultiSegmentRunReport, dest: Path) -> None:
    """Write the multi-segment sidecar to ``dest`` (schema v5)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as f:
        json.dump(_serialise_multi(report), f, indent=2, default=str)
