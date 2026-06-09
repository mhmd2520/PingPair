"""RunReport — the immutable, format-agnostic record of one full sweep.

Every report writer (docx / pdf / txt / xlsx / config-json) takes a
:class:`RunReport` and emits its preferred file format.  The data model is
defined here so adding a new format means writing one new module, no
schema changes.
"""

from __future__ import annotations

import functools
import math
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import AppConfig
from ..core.control.client import (
    MultiSweepResult,
    SweepCaseEntry,
    SweepResult,
    SweepSegment,
)
from ..core.runner import (
    _NO_WINDOW,
    fping_spec,
    iperf3_client_spec,
    iperf3_server_spec,
)
from ..paths import FPING_DIR, FPING_EXE, IPERF3_DIR, IPERF3_EXE


# ---------------------------------------------------------------------------
# Per-case metrics — the shape every writer renders as a row
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CaseMetrics:
    """Flat per-case record. One per row in the report's main table."""

    case_idx: int
    payload_bytes: int
    bandwidth_mbps_pushed: int
    duration_s: int

    # iperf3 client side (the official source of throughput / jitter / loss
    # for the report row).  None when the case errored before iperf3
    # produced output.
    throughput_mbps_received: float | None
    jitter_ms: float | None
    packet_loss_pct: float | None

    # fping summary line (min / avg / max round-trip).
    min_latency_ms: float | None
    avg_latency_ms: float | None
    max_latency_ms: float | None

    status: str           # "ok" / "error"
    error: str | None     # human-readable failure reason if status="error"

    # Forensics — the exact CLI strings used and the subprocess return codes.
    iperf3_client_cmd: str
    iperf3_server_cmd: str
    fping_cmd: str
    iperf3_client_rc: int | None
    fping_rc: int | None
    iperf3_server_rc: int | None
    server_iperf3_json: str = ""    # raw stdout from the remote server


# ---------------------------------------------------------------------------
# RunReport — the whole sweep
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunReport:
    """Top-level container.  Carry-all for every writer."""

    run_id: str                         # filename basename, no extension
    started_at: datetime
    ended_at: datetime
    server_ip: str
    client_ip: str
    # Group F (Q1, 2026-05-16) — profile gateway + per-PC override snapshot.
    # ``gateway`` is the profile-level default (None for point-to-point).
    # ``nic_override`` mirrors the per-PC RunState.nic_override at the time
    # of the sweep (None when the user hadn't applied a custom config).
    # Both are recorded for audit so a reader knows exactly what IP /
    # subnet / gateway were in play when the run happened.
    gateway: str | None = None
    nic_override: dict[str, Any] | None = None
    protocol: str = "udp"
    fping_version: str = ""
    iperf3_version: str = ""
    app_version: str = ""
    cfg_snapshot: dict[str, Any] = field(default_factory=dict)
    cases: list[CaseMetrics] = field(default_factory=list)
    # Test-record metadata: technician, customer, hardware S/N, environment,
    # record_id. Set by the Save Options tab; ends up on the docx title page,
    # the xlsx Run-info sheet, the pdf cover, and the txt header.
    metadata: dict[str, str] = field(default_factory=dict)
    # Group B: 1-based case indexes the user requested before the sweep
    # started. Empty list = full 20-case run (the canonical sweep).
    # Recorded in the .json sidecar so a partial run is auditable
    # — readers can tell "user wanted 12 cases, all 12 ran" apart from
    # "user wanted 20, only 12 finished before stop".
    selected_case_indexes: list[int] = field(default_factory=list)
    # Optional physical cable length under test, in metres, as the user typed
    # it ("12.50"); "" = not provided. Rendered via metadata_rows().
    cable_length_m: str = ""

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def cases_ok(self) -> int:
        return sum(1 for c in self.cases if c.status == "ok")

    @property
    def cases_total(self) -> int:
        return len(self.cases)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_FPING_VERSION_RE = re.compile(r"[Vv]ersion\s+(\d+(?:\.\d+)+)")
_IPERF3_VERSION_RE = re.compile(r"\biperf\s+(\d+(?:\.\d+)+)", re.IGNORECASE)


def _extract_fping_version(raw: str) -> str:
    """Pull just ``X.Y`` out of fping's --version line.

    fping prints something like ``/fping/fping: Version 5.5`` where the
    prefix is ``argv[0]`` (path + name) and varies with how the binary was
    invoked. The version number is the only useful bit for the report.
    Falls back to the trimmed raw line if the pattern doesn't match.
    """
    m = _FPING_VERSION_RE.search(raw)
    if m:
        return m.group(1)
    return raw.strip() or "unknown"


def _extract_iperf3_version(raw: str) -> str:
    """Pull just ``X.Y`` out of iperf3's --version line.

    iperf3 prints ``iperf 3.21 (cJSON 1.7.15)`` followed by build details.
    We only render the version number; the cJSON suffix + uname line bloat
    the report without telling the operator anything useful.
    Falls back to the trimmed raw line if the pattern doesn't match.
    """
    m = _IPERF3_VERSION_RE.search(raw)
    if m:
        return m.group(1)
    return raw.strip() or "unknown"


@functools.lru_cache(maxsize=1)
def _probe_versions() -> tuple[str, str]:
    """Return (fping_version, iperf3_version) by probing the bundled binaries.

    Best-effort: if a probe fails for any reason we return ``"unknown"`` so
    report generation never blocks on a binary glitch. Output is normalised
    to just the bare version number (e.g. ``"5.5"`` / ``"3.21"``) via
    ``_extract_fping_version`` / ``_extract_iperf3_version`` so the docx /
    pdf / txt headers stay scannable.

    Cached (``lru_cache``) — the bundled binaries don't change for the life
    of the process, so the two probe subprocesses run once instead of on
    every report build (each was a blocking call of up to 3 s).
    """
    fping_v = "unknown"
    iperf3_v = "unknown"
    try:
        out = subprocess.run(
            [str(FPING_EXE), "-v"],
            cwd=str(FPING_DIR),
            capture_output=True, text=True, errors="replace",
            timeout=3, check=False, creationflags=_NO_WINDOW,
        )
        text = (out.stdout or "") + (out.stderr or "")
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        fping_v = _extract_fping_version(first_line) if first_line else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        out = subprocess.run(
            [str(IPERF3_EXE), "-v"],
            cwd=str(IPERF3_DIR),
            capture_output=True, text=True, errors="replace",
            timeout=3, check=False, creationflags=_NO_WINDOW,
        )
        text = (out.stdout or "") + (out.stderr or "")
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        iperf3_v = _extract_iperf3_version(first_line) if first_line else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return fping_v, iperf3_v


def _finite_or_none(value: float | None) -> float | None:
    """Collapse a non-finite float (NaN / ±inf) to ``None``.

    fping emits no min/avg/max block on a 100%-loss case, so the parser
    sets those latencies to ``float("nan")`` (a tested, intended outcome).
    Without this boundary guard the NaN renders as the literal string
    ``nan`` in docx/pdf/txt latency cells, writes a fragile ``nan`` into
    xlsx, serialises as the non-strict JSON token ``NaN`` in the sidecar,
    and poisons the appendix mean/median/stdev for the whole run. Every
    writer already treats ``None`` as "—" / empty, so normalising here
    fixes all of those at once.
    """
    if value is None or not math.isfinite(value):
        return None
    return value


def _entry_to_metrics(entry: SweepCaseEntry, cfg: AppConfig) -> CaseMetrics:
    case = entry.case
    cr = entry.case_result

    iperf = cr.iperf_client if cr is not None else None
    fp = cr.fping if cr is not None else None

    iperf_client_cmd = iperf3_client_spec(cfg, case, loopback=False).command_string
    iperf_server_cmd = iperf3_server_spec(cfg, loopback=False, json=True).command_string
    fping_cmd_str = fping_spec(cfg, case, loopback=False).command_string

    return CaseMetrics(
        case_idx=case.index,
        payload_bytes=case.payload_bytes,
        bandwidth_mbps_pushed=case.bandwidth_mbps,
        duration_s=case.duration_s,
        throughput_mbps_received=_finite_or_none(iperf.throughput_mbps) if iperf else None,
        jitter_ms=_finite_or_none(iperf.jitter_ms) if iperf else None,
        packet_loss_pct=_finite_or_none(iperf.packet_loss_pct) if iperf else None,
        min_latency_ms=_finite_or_none(fp.min_ms) if fp else None,
        avg_latency_ms=_finite_or_none(fp.avg_ms) if fp else None,
        max_latency_ms=_finite_or_none(fp.max_ms) if fp else None,
        status="ok" if entry.ok else "error",
        error=entry.error,
        iperf3_client_cmd=iperf_client_cmd,
        iperf3_server_cmd=iperf_server_cmd,
        fping_cmd=fping_cmd_str,
        iperf3_client_rc=cr.iperf_client_run.returncode if cr else None,
        fping_rc=cr.fping_run.returncode if cr else None,
        iperf3_server_rc=entry.server_returncode,
        server_iperf3_json=entry.server_iperf3_json,
    )


def build_run_report(
    sweep: SweepResult,
    cfg: AppConfig,
    *,
    run_id: str | None = None,
    metadata: dict[str, str] | None = None,
    selected_case_indexes: list[int] | None = None,
    cable_length_m: str = "",
) -> RunReport:
    """Flatten a SweepResult + AppConfig into a RunReport ready for any writer.

    ``selected_case_indexes`` (Group B) records what the user asked for
    before the sweep started. Empty / None = a full 20-case run.
    ``cable_length_m`` is the optional Run-tab cable length (metres, as typed).
    """
    started = datetime.fromtimestamp(sweep.started_at)
    ended = datetime.fromtimestamp(sweep.ended_at or sweep.started_at)
    if run_id is None:
        run_id = "PingPair_" + started.strftime("%Y-%m-%d_%H%M")

    fping_v, iperf3_v = _probe_versions()

    return RunReport(
        run_id=run_id,
        started_at=started,
        ended_at=ended,
        server_ip=str(cfg.network.server_ip),
        client_ip=str(cfg.network.client_ip),
        gateway=str(cfg.network.gateway) if cfg.network.gateway else None,
        protocol=cfg.test_plan.protocol,
        fping_version=fping_v,
        iperf3_version=iperf3_v,
        app_version=__version__,
        cfg_snapshot=cfg.model_dump(mode="json"),
        cases=[_entry_to_metrics(e, cfg) for e in sweep.cases],
        metadata=dict(metadata or {}),
        selected_case_indexes=list(selected_case_indexes or []),
        cable_length_m=str(cable_length_m or "").strip(),
    )


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def _sanitize_basename(name: str) -> str:
    """Reduce a rendered filename to a single safe path component.

    The filename *pattern* is user-editable (Save Options / an imported
    config) and the result is used as the per-sweep **folder name** joined to
    the Reports dir — and PingPair runs **elevated**. An unsanitised pattern
    like ``..\\..\\Windows\\Temp\\x`` or ``C:\\evil`` would let the sweep
    write its folder outside the Reports tree (CWE-22). Keep only the final
    path segment, drop drive/UNC prefixes and ``..``, and replace characters
    illegal in a Windows filename. Falls back to ``PingPair`` if nothing safe
    remains.
    """
    # Collapse both separators, take the LAST segment (kills ../ and C:\ etc.).
    last = name.replace("\\", "/").split("/")[-1]
    # Strip leading/trailing dots and whitespace (".." → "", trailing-dot dirs).
    last = last.strip().strip(". ")
    # Replace characters illegal on Windows / reserved by the shell.
    last = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", last)
    return last or "PingPair"


def render_filename(pattern: str, started: datetime) -> str:
    """Resolve `{date}` / `{time}` tokens in the user's filename pattern.

    Example: ``PingPair_{date}_{time}`` → ``PingPair_2026-05-09_0947``

    A custom pattern with an unknown ``{token}`` or an unbalanced brace
    makes ``str.format`` raise — rather than crash report saving we fall
    back to a plain substitution of the two known tokens, leaving any
    stray braces as literal text.

    The result is always reduced to one safe filename component (see
    :func:`_sanitize_basename`) — it becomes a folder name and the app runs
    elevated, so a traversal/absolute pattern must not escape the Reports dir.
    """
    fields = {
        "date": started.strftime("%Y-%m-%d"),
        "time": started.strftime("%H%M"),  # HHMM only — seconds dropped 2026-05-12
    }
    try:
        result = pattern.format(**fields)
    except (KeyError, IndexError, ValueError):
        result = pattern
        for key, val in fields.items():
            result = result.replace("{" + key + "}", val)
    return _sanitize_basename(result)


def unique_basename(dest_dir: Path, basename: str) -> str:
    """Return ``basename`` if ``dest_dir / basename`` is free; else append ``_2``, ``_3``, …

    The default filename pattern (``PingPair_{date}_{time}``) is
    naturally unique per run, but a custom pattern like ``"test"`` or
    ``"M2-M4-baseline"`` will collide on the second run. Rather than
    overwrite, we suffix:

      * ``test`` → ``test`` (first run, folder doesn't exist)
      * ``test`` → ``test_2`` (folder ``test`` exists)
      * ``test`` → ``test_3`` (both ``test`` and ``test_2`` exist)
      * ``test`` → ``test`` (if ``test_2`` exists but ``test`` itself
        is free — e.g. user manually deleted the original — we take
        the un-suffixed slot back)

    ``Path.exists`` returns True for files OR folders with that name,
    so this also protects against an accidental file-of-the-same-name
    collision in the destination directory.
    """
    if not (dest_dir / basename).exists():
        return basename
    n = 2
    while (dest_dir / f"{basename}_{n}").exists():
        n += 1
    return f"{basename}_{n}"


def fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as ``Mm Ss``.

    Reports always show wall-clock time as minutes + seconds rather than
    a single floating-point number — easier to scan, no need to mentally
    convert "950.2 s" into "almost 16 minutes".
    """
    if seconds < 0:
        seconds = 0.0
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    if minutes == 0:
        return f"{secs}s"
    return f"{minutes}m {secs}s"


# Display labels for the metadata dict keys, in the order they appear in
# every report.  Centralised so writers stay in sync.
METADATA_LABELS: tuple[tuple[str, str], ...] = (
    ("technician", "Technician"),
    ("customer", "Customer / Site"),
    ("hardware_sn", "Hardware S/N"),
    ("environment", "Test environment"),
    ("record_id", "Test record ID"),
)


def metadata_rows(report: RunReport | "MultiSegmentRunReport") -> list[tuple[str, str]]:
    """Return only the populated metadata fields, as (label, value) pairs.

    Empty fields are omitted so a sweep run without filling out the
    Save Options tab's metadata section just gets the standard run header.

    Accepts either ``RunReport`` (single sweep) or
    ``MultiSegmentRunReport`` (Group C-1) — both carry the same
    ``metadata`` dict shape.
    """
    out: list[tuple[str, str]] = []
    for key, label in METADATA_LABELS:
        # str() guard: metadata is loaded from a JSON sidecar that a future
        # writer (or a hand-edit) could populate with a non-string value;
        # ``.strip()`` on, say, an int would raise and break report rendering.
        value = str((report.metadata or {}).get(key, "")).strip()
        if value:
            out.append((label, value))
    # Cable length is a dedicated report field (not part of the editable
    # metadata dict), appended here so it renders in every format alongside
    # the metadata rows. Only shown when the operator entered a value.
    cable = str(getattr(report, "cable_length_m", "") or "").strip()
    if cable:
        out.append(("Cable length (m)", cable))
    return out


# ---------------------------------------------------------------------------
# Group C-1: multi-segment reports
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SegmentMetrics:
    """Flattened per-segment record used by the multi-segment writers.

    Mirrors :class:`CaseMetrics` for the per-segment cases plus a
    handful of segment-level fields (label, status, timing) so writers
    don't have to crawl the original :class:`SweepSegment` tree at
    render time.
    """

    segment_idx: int                    # 1-based
    label: str                          # operator-supplied; "Segment N" if blank
    started_at: datetime
    ended_at: datetime
    status: str                         # "ok" | "partial" | "failed"
    error: str                          # human-readable detail when not "ok"
    cases: list[CaseMetrics] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def cases_ok(self) -> int:
        return sum(1 for c in self.cases if c.status == "ok")

    @property
    def cases_total(self) -> int:
        return len(self.cases)


@dataclass(slots=True)
class MultiSegmentRunReport:
    """Container for a multi-segment run — N segments × M cases each.

    Distinct from :class:`RunReport` so the writers can dispatch
    cleanly via ``isinstance``. The shared fields (server/client IPs,
    metadata, version strings) are copied to the top level since
    they're constant across segments; per-segment timing and case
    lists live in :class:`SegmentMetrics`.
    """

    run_id: str                         # filename basename, no extension
    started_at: datetime
    ended_at: datetime
    server_ip: str
    client_ip: str
    # Group F (Q1, 2026-05-16) — profile gateway + per-PC override
    # snapshot, mirroring :class:`RunReport`. Defaults allow callers
    # that don't pass these to keep working. Round-9 II fix
    # (2026-05-17): the original Group F sweep added these to RunReport
    # but the .split() heuristic skipped this class because it found
    # only one matching field signature after RunReport was patched.
    gateway: str | None = None
    nic_override: dict[str, Any] | None = None
    protocol: str = "udp"
    fping_version: str = ""
    iperf3_version: str = ""
    app_version: str = ""
    cfg_snapshot: dict[str, Any] = field(default_factory=dict)
    segments: list[SegmentMetrics] = field(default_factory=list)
    selected_case_indexes: list[int] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    # Optional physical cable length under test, in metres ("12.50"); "" = unset.
    cable_length_m: str = ""

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def segments_ok(self) -> int:
        return sum(1 for s in self.segments if s.status == "ok")

    @property
    def segments_total(self) -> int:
        return len(self.segments)

    @property
    def total_cases(self) -> int:
        return sum(s.cases_total for s in self.segments)

    @property
    def total_cases_ok(self) -> int:
        return sum(s.cases_ok for s in self.segments)


def _segment_to_metrics(
    segment: SweepSegment, cfg: AppConfig
) -> SegmentMetrics:
    """Flatten a SweepSegment into the report-friendly SegmentMetrics."""
    sweep = segment.sweep
    started = datetime.fromtimestamp(sweep.started_at)
    ended = datetime.fromtimestamp(sweep.ended_at or sweep.started_at)
    return SegmentMetrics(
        segment_idx=segment.segment_idx,
        label=segment.label or f"Segment {segment.segment_idx}",
        started_at=started,
        ended_at=ended,
        status=segment.status,
        error=segment.error,
        cases=[_entry_to_metrics(e, cfg) for e in sweep.cases],
    )


def build_multi_run_report(
    multi: MultiSweepResult,
    cfg: AppConfig,
    *,
    run_id: str | None = None,
    metadata: dict[str, str] | None = None,
    cable_length_m: str = "",
) -> MultiSegmentRunReport:
    """Flatten a MultiSweepResult + AppConfig into a MultiSegmentRunReport.

    Mirrors :func:`build_run_report` but iterates ``multi.segments``.
    ``selected_case_indexes`` is carried through so the sidecar can
    record what subset the operator picked at the start of the
    multi-segment run.
    """
    started = datetime.fromtimestamp(multi.started_at)
    ended = datetime.fromtimestamp(multi.ended_at or multi.started_at)
    if run_id is None:
        run_id = "PingPair_" + started.strftime("%Y-%m-%d_%H%M") + "_multisegment"

    fping_v, iperf3_v = _probe_versions()

    return MultiSegmentRunReport(
        run_id=run_id,
        started_at=started,
        ended_at=ended,
        server_ip=str(cfg.network.server_ip),
        client_ip=str(cfg.network.client_ip),
        gateway=str(cfg.network.gateway) if cfg.network.gateway else None,
        protocol=cfg.test_plan.protocol,
        fping_version=fping_v,
        iperf3_version=iperf3_v,
        app_version=__version__,
        cfg_snapshot=cfg.model_dump(mode="json"),
        segments=[_segment_to_metrics(s, cfg) for s in multi.segments],
        selected_case_indexes=list(multi.selected_case_indexes or []),
        metadata=dict(metadata or {}),
        cable_length_m=str(cable_length_m or "").strip(),
    )


def segment_summary_rows(
    report: MultiSegmentRunReport,
) -> list[tuple[str, str, str, str, str, str]]:
    """Return rows for the top-of-report Segments summary table.

    Six columns: # / Label / Started / Duration / Cases ok/total /
    Status. All values are pre-formatted strings so writers don't
    re-implement formatting per format.
    """
    rows: list[tuple[str, str, str, str, str, str]] = []
    for seg in report.segments:
        rows.append((
            str(seg.segment_idx),
            seg.label,
            seg.started_at.strftime("%H:%M:%S"),
            fmt_duration(seg.duration_s),
            f"{seg.cases_ok}/{seg.cases_total}",
            seg.status.upper(),
        ))
    return rows


# Headers for the segment summary table — single source of truth so
# every writer formats them identically.
SEGMENT_SUMMARY_HEADERS: tuple[str, ...] = (
    "#", "Label", "Started", "Duration", "Cases (ok/total)", "Status",
)


def cross_segment_comparison(
    report: MultiSegmentRunReport,
    *,
    metric: str = "throughput",
    as_values: bool = False,
) -> tuple[list[str], list[tuple]]:
    """Build a comparison table for ``metric`` across all segments.

    Returns ``(headers, rows)`` where ``headers`` is
    ``["Case #", "Payload (B)", "BW (Mbps)", <segment1 label>, …]``
    and each row is a tuple — case identifying columns followed by the
    metric value per segment (or a missing-value placeholder if the case
    didn't run in that segment).

    ``metric`` is one of: ``"throughput"`` (Mbps),
    ``"avg_latency"`` (ms), ``"loss"`` (%), ``"jitter"`` (ms). The
    docx / xlsx / pdf / txt writers each call this for the metric
    they care about.

    With ``as_values=False`` (default) the data cells are formatted
    strings (``"—"`` for a missing case). With ``as_values=True`` they
    are raw ``float | None`` and the case#/payload/bw columns are ``int``
    — the xlsx writer uses this so its native charts plot full-precision
    numbers instead of values round-tripped through a 2/3-decimal string.
    """
    # Build header.
    headers: list[str] = ["Case #", "Payload (B)", "BW (Mbps)"]
    for seg in report.segments:
        headers.append(seg.label)

    # Collect every unique case — keyed by its 1-based ``case_idx`` — that
    # appears in any segment, ordered by first appearance. ``case_idx`` is
    # the stable identity: the shared-subset rule (Group C-1) means every
    # segment reruns the same filtered plan, so this is normally just
    # segment 0's cases; the guard handles partial / failed segments that
    # are missing a case. Keying by ``case_idx`` rather than the
    # (payload, bandwidth) pair means a segment is never collapsed when it
    # repeats a pair, and the "Case #" column stays correct across every
    # segment. Building a per-segment ``{case_idx: case}`` index up front
    # also turns the previously quadratic per-cell scan into an O(1) lookup.
    seg_index: list[dict[int, CaseMetrics]] = [
        {case.case_idx: case for case in seg.cases}
        for seg in report.segments
    ]
    seen: set[int] = set()
    canonical_order: list[CaseMetrics] = []
    for seg in report.segments:
        for case in seg.cases:
            if case.case_idx in seen:
                continue
            seen.add(case.case_idx)
            canonical_order.append(case)

    # Per-metric decimal precision — jitter and loss are small fractional
    # values shown at 3 places elsewhere; throughput / latency at 2. Keeps
    # this comparison table consistent with the per-segment metric tables.
    dec = {"throughput": 2, "avg_latency": 2, "loss": 3, "jitter": 3}.get(metric, 2)
    # ``None`` for an unrecognised metric — every cell then resolves to the
    # missing-value placeholder, matching the pre-refactor behaviour.
    attr = {
        "throughput": "throughput_mbps_received",
        "avg_latency": "avg_latency_ms",
        "loss": "packet_loss_pct",
        "jitter": "jitter_ms",
    }.get(metric)

    # Build rows.
    rows: list[tuple] = []
    for canonical in canonical_order:
        if as_values:
            row: list = [
                canonical.case_idx,
                canonical.payload_bytes,
                canonical.bandwidth_mbps_pushed,
            ]
        else:
            row = [
                str(canonical.case_idx),
                str(canonical.payload_bytes),
                str(canonical.bandwidth_mbps_pushed),
            ]
        for idx_map in seg_index:
            case = idx_map.get(canonical.case_idx)
            raw: float | None = (
                getattr(case, attr) if (case is not None and attr is not None)
                else None
            )
            if as_values:
                row.append(raw)
            else:
                row.append("—" if raw is None else f"{raw:.{dec}f}")
        rows.append(tuple(row))
    return headers, rows
