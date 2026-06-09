"""Parse ``*.json`` sweep sidecars (schema v3-v5) into a uniform shape.

The Analysis tab overlays metrics from one or more past sweeps. Each
sweep lives on disk as a JSON sidecar next to its docx/xlsx/pdf/txt
report files. The loader handles every shape PingPair has ever written:

* **v3** — Group B: flat ``cases`` list plus ``metadata`` and
  ``selected_case_indexes``.
* **v4** — Group C-1: replaces the flat ``cases`` list with a
  ``segments: [{cases: [...]}, …]`` block — one segment per
  continuous-mode pass.
* **v5** — Group F (Q1, 2026-05-16): adds ``gateway`` + ``nic_override``
  on top of v3/v4.

This module hides those differences behind two dataclasses:

:class:`CasePoint`
    A single (case_idx, payload, bandwidth) → metric row.

:class:`LoadedRun`
    A whole sidecar plus one-or-more :class:`Series` objects. A
    single-segment sidecar always produces exactly one series labeled
    with the run_id; a multi-segment sidecar produces one series per
    segment labeled ``<run_id> · <segment_label>``.

The Analysis view never has to ask "which schema is this?" — every
loaded run iterates the same way.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SidecarParseError(ValueError):
    """Raised when a sidecar file can't be parsed into a :class:`LoadedRun`.

    Wraps the underlying cause (``OSError``, ``json.JSONDecodeError``,
    ``KeyError``, etc.) so the view layer can show one friendly message
    instead of branching on every possible IO/JSON failure.
    """


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CasePoint:
    """One plotted point per case per metric.

    Mirrors the subset of :class:`pingpair.reporting.run_report.CaseMetrics`
    the Analysis tab actually charts — everything else (CLI strings,
    return codes, raw iperf3 JSON) is dropped to keep loaded-run
    memory footprint small even when comparing 20 runs.
    """

    case_idx: int
    payload_bytes: int
    bandwidth_mbps_pushed: int
    status: str                                 # "ok" | "error"

    throughput_mbps_received: float | None
    jitter_ms: float | None
    packet_loss_pct: float | None
    avg_latency_ms: float | None
    min_latency_ms: float | None
    max_latency_ms: float | None


@dataclass(slots=True)
class Series:
    """One plot line on the Analysis charts.

    A single-segment run (v1-v3 sidecar) loads as one Series; a
    multi-segment run (v4 sidecar) loads as one Series per segment.
    Each chart's x = case_idx, y = the matching field on the
    CasePoint.
    """

    label: str                                  # "<run_id>" or "<run_id> · <seg_label>"
    cases: list[CasePoint] = field(default_factory=list)

    @property
    def cases_ok(self) -> int:
        return sum(1 for c in self.cases if c.status == "ok")

    @property
    def cases_total(self) -> int:
        return len(self.cases)


@dataclass(slots=True)
class LoadedRun:
    """A whole sidecar's worth of data, schema-agnostic.

    ``path`` is the source ``.json`` so the view can show a tooltip /
    open-folder action. ``display_label`` is the user-facing short
    label shown in the loaded-runs list — defaults to ``run_id``, can
    be overridden via the Rename button on the Analysis tab.
    """

    path: Path
    run_id: str
    display_label: str
    schema_version: int
    started_at: datetime | None
    duration_s: float
    server_ip: str
    client_ip: str
    protocol: str
    is_multi_segment: bool
    metadata: dict[str, str] = field(default_factory=dict)
    series: list[Series] = field(default_factory=list)

    @property
    def cases_total(self) -> int:
        return sum(s.cases_total for s in self.series)

    @property
    def cases_ok(self) -> int:
        return sum(s.cases_ok for s in self.series)

    def summary_line(self) -> str:
        """One-liner shown next to the checkbox in the runs list."""
        when = (
            self.started_at.strftime("%Y-%m-%d %H:%M")
            if self.started_at is not None
            else "?"
        )
        tag = "multi" if self.is_multi_segment else "single"
        return (
            f"{self.display_label}  ·  {when}  ·  "
            f"{self.cases_ok}/{self.cases_total} ok  ·  {tag}"
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _opt_float(value: Any) -> float | None:
    """JSON's ``null`` decodes to Python ``None``; everything else → float.

    Defensive: an old sidecar with the value written as a string
    (shouldn't happen, but might if a future writer slips up) still
    gets coerced rather than blowing up the whole load.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any, default: int = 0) -> int:
    """Coerce a JSON value to int, falling back to ``default``.

    Mirrors :func:`_opt_float`'s tolerance: a present-but-null or
    non-numeric field (corrupt / hand-edited sidecar) yields ``default``
    rather than raising ``TypeError``/``ValueError`` — without this, a
    single bad case would abort the whole Analysis-tab folder scan
    (``load_many`` only swallows :class:`SidecarParseError`).
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_case(raw: dict[str, Any]) -> CasePoint:
    """Lift one case dict (v1-v4, same shape) into a :class:`CasePoint`."""
    return CasePoint(
        case_idx=_opt_int(raw.get("case_idx")),
        payload_bytes=_opt_int(raw.get("payload_bytes")),
        bandwidth_mbps_pushed=_opt_int(raw.get("bandwidth_mbps_pushed")),
        status=str(raw.get("status", "error")),
        throughput_mbps_received=_opt_float(raw.get("throughput_mbps_received")),
        jitter_ms=_opt_float(raw.get("jitter_ms")),
        packet_loss_pct=_opt_float(raw.get("packet_loss_pct")),
        avg_latency_ms=_opt_float(raw.get("avg_latency_ms")),
        min_latency_ms=_opt_float(raw.get("min_latency_ms")),
        max_latency_ms=_opt_float(raw.get("max_latency_ms")),
    )


def _parse_iso(value: Any) -> datetime | None:
    """Tolerantly parse an ISO-format timestamp string.

    Anything unparseable returns ``None`` rather than raising — the
    Analysis tab shows ``"?"`` in the summary line instead of refusing
    to load the run.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# Largest sidecar we will read into memory. A real ``.json`` sidecar for
# a 20-case sweep is a few hundred KB; 32 MiB is a generous ceiling that
# stops a corrupt or pathologically large file from slurping RAM during
# an Analysis-tab scan.
_MAX_SIDECAR_BYTES = 32 * 1024 * 1024


def load_sidecar(path: Path) -> LoadedRun:
    """Parse a single sweep ``.json`` file into a :class:`LoadedRun`.

    Raises :class:`SidecarParseError` on any failure (file missing,
    JSON malformed, schema_version unrecognised, file implausibly large).
    The caller can either show a per-file warning and continue or abort
    the whole scan.

    The schema_version field is *advisory* — we always try to find
    cases under either the multi-segment ``segments`` key or the
    single-segment ``cases`` key, regardless of what the version field
    says. That way a sidecar written by a slightly-newer PingPair with
    a higher schema_version but the same fields still loads cleanly.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SidecarParseError(f"could not read {path}: {exc}") from exc
    if size > _MAX_SIDECAR_BYTES:
        raise SidecarParseError(
            f"{path}: {size} bytes exceeds the {_MAX_SIDECAR_BYTES}-byte "
            "sidecar limit — refusing to load"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SidecarParseError(f"could not read {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarParseError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SidecarParseError(
            f"{path}: top-level JSON value must be an object, got "
            f"{type(data).__name__}"
        )

    # Coerce the top-level scalars through the SAME tolerant helpers the
    # per-case fields use. A string/garbage ``schema_version`` or
    # ``duration_s`` in a hand-edited sidecar would otherwise raise a bare
    # ValueError here — OUTSIDE any try — escaping the SidecarParseError
    # contract, so ``load_many``'s ``except SidecarParseError`` wouldn't
    # catch it and one bad file would abort the whole folder scan (the exact
    # failure this module's tolerant design is meant to prevent).
    schema = _opt_int(data.get("schema_version", 1), default=1)
    run_id = str(data.get("run_id", path.stem.replace(".config", "")))
    started_at = _parse_iso(data.get("started_at"))
    duration_s = _opt_float(data.get("duration_s")) or 0.0
    server_ip = str(data.get("server_ip", ""))
    client_ip = str(data.get("client_ip", ""))
    protocol = str(data.get("protocol", "udp"))
    metadata_raw = data.get("metadata") or {}
    metadata = (
        {str(k): str(v) for k, v in metadata_raw.items()}
        if isinstance(metadata_raw, dict)
        else {}
    )

    # Two shapes — multi-segment uses segments[], single-segment uses
    # a flat cases[].
    series: list[Series] = []
    if isinstance(data.get("segments"), list) and data["segments"]:
        is_multi = True
        for seg_raw in data["segments"]:
            if not isinstance(seg_raw, dict):
                continue
            seg_label = str(
                seg_raw.get("label")
                or f"Segment {seg_raw.get('segment_idx', '?')}"
            )
            cases_raw = seg_raw.get("cases") or []
            cases = [
                _parse_case(c)
                for c in cases_raw
                if isinstance(c, dict)
            ]
            series.append(
                Series(
                    label=f"{run_id} · {seg_label}",
                    cases=cases,
                )
            )
    else:
        is_multi = False
        cases_raw = data.get("cases") or []
        cases = [
            _parse_case(c)
            for c in cases_raw
            if isinstance(c, dict)
        ]
        # Single-segment runs always produce exactly one Series so the
        # downstream "iterate series" loop stays uniform.
        series.append(Series(label=run_id, cases=cases))

    return LoadedRun(
        path=path,
        run_id=run_id,
        display_label=run_id,
        schema_version=schema,
        started_at=started_at,
        duration_s=duration_s,
        server_ip=server_ip,
        client_ip=client_ip,
        protocol=protocol,
        is_multi_segment=is_multi,
        metadata=metadata,
        series=series,
    )


# ---------------------------------------------------------------------------
# Folder scanner
# ---------------------------------------------------------------------------


def enumerate_sidecars(root: Path) -> list[Path]:
    """Return every sweep sidecar under ``root`` (newest first).

    Each sweep lives in its own subfolder named after the run; the
    sidecar inside is ``<basename>/<basename>.json``. The match is
    deliberately tight so unrelated ``.json`` files in the Reports
    tree (user notes, profile copies, etc.) are ignored.

    Returns an empty list if ``root`` doesn't exist or isn't a directory
    — never raises — so the view layer can call this on a stale
    Destination path without crashing.

    Sorted by ``st_mtime`` descending so the newest sweep is first in
    the Analysis tab's loaded-runs list, matching the Save Options tab's
    Recent reports section.
    """
    if not root.is_dir():
        return []

    found: list[Path] = []
    try:
        for sub in root.iterdir():
            if sub.is_dir():
                candidate = sub / f"{sub.name}.json"
                if candidate.is_file():
                    found.append(candidate)
    except OSError:
        return []

    # Sort newest-first by mtime. Stat each file exactly once, in a
    # single pass — the previous ``sort(key=lambda: p.stat()...)`` raised
    # on the first stat failure (common on a network share) and left the
    # *whole* list unsorted, silently breaking the "newest first"
    # contract. Now a failed stat only drops that one file to the end.
    with_mtime: list[tuple[float, Path]] = []
    no_mtime: list[Path] = []
    for p in found:
        try:
            with_mtime.append((p.stat().st_mtime, p))
        except OSError:
            no_mtime.append(p)
    with_mtime.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in with_mtime] + no_mtime


def load_many(
    paths: Iterable[Path],
    *,
    on_error: Callable[[Path, SidecarParseError], None] | None = None,
) -> list[LoadedRun]:
    """Batch-load a list of sidecars, calling ``on_error`` for each failure.

    Skips files that fail to parse rather than aborting the whole load,
    so a single corrupt sidecar in a Reports folder doesn't hide every
    other run from the Analysis tab. If ``on_error`` is provided, it's
    invoked with ``(path, exception)`` per failure so the caller can
    surface a per-file warning in the UI.
    """
    loaded: list[LoadedRun] = []
    for p in paths:
        try:
            loaded.append(load_sidecar(p))
        except SidecarParseError as exc:
            if on_error is not None:
                on_error(p, exc)
    return loaded
