"""Group C-2 — sidecar loader tests.

Covers :mod:`pingpair.analysis.sidecar_loader`:

* round-trip a v3 single-segment sidecar → :class:`LoadedRun` with
  one :class:`Series`.
* round-trip a v4 multi-segment sidecar → one :class:`Series` per
  segment, labelled ``"<run_id> · <seg_label>"``.
* tolerate v1 / v2 sidecars that pre-date the ``metadata`` and
  ``selected_case_indexes`` keys.
* resilience: malformed JSON / wrong top-level type / unreadable
  file all raise :class:`SidecarParseError`.
* :func:`enumerate_sidecars` finds the per-sweep ``<sub>/<sub>.json``
  layout, ignores unrelated ``.json`` files, and is sorted newest-first.
* :func:`load_many` collects errors via the ``on_error`` callback and
  still returns the successfully-loaded runs.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from pingpair.analysis import (
    LoadedRun,
    Series,
    SidecarParseError,
    enumerate_sidecars,
    load_many,
    load_sidecar,
)


# ---------------------------------------------------------------------------
# Sample sidecars (real shape — mirrors what config_writer.py emits)
# ---------------------------------------------------------------------------


def _case(idx: int, payload: int, bw: int, *, ok: bool = True) -> dict:
    return {
        "case_idx": idx,
        "payload_bytes": payload,
        "bandwidth_mbps_pushed": bw,
        "duration_s": 30,
        "throughput_mbps_received": float(bw) if ok else None,
        "jitter_ms": 0.05 if ok else None,
        "packet_loss_pct": 0.0 if ok else None,
        "min_latency_ms": 0.10 if ok else None,
        "avg_latency_ms": 0.40 if ok else None,
        "max_latency_ms": 2.10 if ok else None,
        "status": "ok" if ok else "error",
        "error": None if ok else "synthetic failure",
        # Fields the loader doesn't care about but the sidecar carries:
        "iperf3_client_cmd": "iperf3 -c 192.168.1.1",
        "iperf3_server_cmd": "iperf3 -s",
        "fping_cmd": "fping 192.168.1.1",
        "iperf3_client_rc": 0 if ok else 1,
        "fping_rc": 0,
        "iperf3_server_rc": 0,
        "server_iperf3_json": "",
    }


def _v3_sidecar(run_id: str = "PingTool_2026-05-09_120000") -> dict:
    return {
        "schema_version": 3,
        "run_id": run_id,
        "started_at": "2026-05-09T12:00:00",
        "ended_at": "2026-05-09T12:16:00",
        "duration_s": 960.0,
        "server_ip": "192.168.1.1",
        "client_ip": "192.168.1.2",
        "protocol": "udp",
        "fping_version": "fping: Version 4.2",
        "iperf3_version": "iperf 3.14",
        "app_version": "0.5.0",
        "cases_ok": 3,
        "cases_total": 3,
        "metadata": {"technician": "Mohamed Khaled", "customer": "ACME Rail"},
        "selected_case_indexes": [],
        "cfg_snapshot": {"network": {"server_ip": "192.168.1.1"}},
        "cases": [
            _case(1, 200, 10),
            _case(2, 200, 30),
            _case(3, 200, 50, ok=False),
        ],
    }


def _v4_sidecar(run_id: str = "PingTool_2026-05-10_140000_multisegment") -> dict:
    return {
        "schema_version": 4,
        "run_id": run_id,
        "started_at": "2026-05-10T14:00:00",
        "ended_at": "2026-05-10T14:48:00",
        "duration_s": 2880.0,
        "server_ip": "192.168.1.1",
        "client_ip": "192.168.1.2",
        "protocol": "udp",
        "fping_version": "fping: Version 4.2",
        "iperf3_version": "iperf 3.14",
        "app_version": "0.5.0",
        "segments_ok": 2,
        "segments_total": 2,
        "total_cases_ok": 4,
        "total_cases": 4,
        "metadata": {"technician": "Mohamed Khaled"},
        "selected_case_indexes": [],
        "cfg_snapshot": {},
        "segments": [
            {
                "segment_idx": 1,
                "label": "M2-M4",
                "started_at": "2026-05-10T14:00:00",
                "ended_at": "2026-05-10T14:24:00",
                "duration_s": 1440.0,
                "status": "ok",
                "error": "",
                "cases_ok": 2,
                "cases_total": 2,
                "cases": [_case(1, 200, 10), _case(2, 200, 30)],
            },
            {
                "segment_idx": 2,
                "label": "M4-M6",
                "started_at": "2026-05-10T14:24:00",
                "ended_at": "2026-05-10T14:48:00",
                "duration_s": 1440.0,
                "status": "ok",
                "error": "",
                "cases_ok": 2,
                "cases_total": 2,
                "cases": [_case(1, 200, 10), _case(2, 200, 30)],
            },
        ],
    }


def _v1_sidecar() -> dict:
    """Pre-metadata era — no metadata, no selected_case_indexes."""
    return {
        "schema_version": 1,
        "run_id": "PingTool_2026-03-01_090000",
        "started_at": "2026-03-01T09:00:00",
        "ended_at": "2026-03-01T09:16:00",
        "duration_s": 960.0,
        "server_ip": "192.168.1.1",
        "client_ip": "192.168.1.2",
        "protocol": "udp",
        "fping_version": "fping: Version 4.2",
        "iperf3_version": "iperf 3.14",
        "app_version": "0.3.0",
        "cases_ok": 1,
        "cases_total": 1,
        "cfg_snapshot": {},
        "cases": [_case(1, 200, 10)],
    }


# ---------------------------------------------------------------------------
# Single-file round-trip
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


def test_load_v3_single_segment(tmp_path: Path) -> None:
    p = _write(tmp_path, "run_v3.json", _v3_sidecar())
    run = load_sidecar(p)

    assert isinstance(run, LoadedRun)
    assert run.schema_version == 3
    assert run.is_multi_segment is False
    assert run.run_id == "PingTool_2026-05-09_120000"
    assert run.display_label == run.run_id  # defaults to run_id
    assert run.started_at == datetime(2026, 5, 9, 12, 0, 0)
    assert run.duration_s == pytest.approx(960.0)
    assert run.server_ip == "192.168.1.1"
    assert run.protocol == "udp"
    assert run.metadata == {"technician": "Mohamed Khaled", "customer": "ACME Rail"}

    # One series, three cases, last one errored.
    assert len(run.series) == 1
    s = run.series[0]
    assert s.label == run.run_id  # single-segment label = run_id
    assert s.cases_total == 3
    assert s.cases_ok == 2
    assert s.cases[0].throughput_mbps_received == pytest.approx(10.0)
    assert s.cases[2].status == "error"
    assert s.cases[2].throughput_mbps_received is None
    assert s.cases[2].avg_latency_ms is None


def test_load_v4_multi_segment_makes_one_series_per_segment(tmp_path: Path) -> None:
    p = _write(tmp_path, "run_v4_multisegment.json", _v4_sidecar())
    run = load_sidecar(p)

    assert run.schema_version == 4
    assert run.is_multi_segment is True
    assert len(run.series) == 2
    assert run.series[0].label == f"{run.run_id} · M2-M4"
    assert run.series[1].label == f"{run.run_id} · M4-M6"
    # Both segments have the same two cases.
    assert run.cases_total == 4
    assert run.cases_ok == 4


def test_load_v1_sidecar_without_metadata_field(tmp_path: Path) -> None:
    p = _write(tmp_path, "run_v1.json", _v1_sidecar())
    run = load_sidecar(p)

    assert run.schema_version == 1
    assert run.metadata == {}                # missing → empty dict, not crash
    assert run.is_multi_segment is False
    assert run.series[0].cases_total == 1


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_load_missing_file_raises_parse_error(tmp_path: Path) -> None:
    with pytest.raises(SidecarParseError):
        load_sidecar(tmp_path / "nope.json")


def test_load_malformed_json_raises_parse_error(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{not really json", encoding="utf-8")
    with pytest.raises(SidecarParseError):
        load_sidecar(p)


def test_load_non_object_root_raises_parse_error(tmp_path: Path) -> None:
    """A JSON list at the root is valid JSON but isn't a sidecar."""
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SidecarParseError):
        load_sidecar(p)


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------


def _write_sweep(parent: Path, basename: str, payload: dict) -> Path:
    """Helper: create ``parent/basename/basename.json`` and return the path."""
    sub = parent / basename
    sub.mkdir()
    return _write(sub, f"{basename}.json", payload)


def test_enumerate_sidecars_finds_per_sweep_layout(tmp_path: Path) -> None:
    """The canonical layout is ``<sub>/<sub>.json``."""
    sidecar = _write_sweep(tmp_path, "run-A", _v3_sidecar("run-A"))
    found = enumerate_sidecars(tmp_path)
    assert found == [sidecar]


def test_enumerate_sidecars_sorts_newest_first(tmp_path: Path) -> None:
    older = _write_sweep(tmp_path, "older", _v3_sidecar("older"))
    newer = _write_sweep(tmp_path, "newer", _v3_sidecar("newer"))
    # Force older to have an older mtime regardless of write order.
    past = time.time() - 3600
    os.utime(older, (past, past))

    found = enumerate_sidecars(tmp_path)

    assert found[0] == newer
    assert found[-1] == older


def test_enumerate_sidecars_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert enumerate_sidecars(tmp_path / "does-not-exist") == []


def test_enumerate_sidecars_ignores_unrelated_json_in_sweep_folder(
    tmp_path: Path,
) -> None:
    """A ``.json`` file inside a sweep folder that doesn't match the
    parent's basename is NOT a sidecar (could be user notes, a profile
    copy, etc.) — must not show up in the loaded-runs list."""
    sidecar = _write_sweep(
        tmp_path, "Ping_2026-05-16_0200", _v3_sidecar("real")
    )
    sub = sidecar.parent
    _write(sub, "notes.json", _v3_sidecar("decoy"))
    _write(sub, "other_settings.json", _v3_sidecar("decoy2"))
    _write(tmp_path, "random.json", _v3_sidecar("root_decoy"))
    found = enumerate_sidecars(tmp_path)
    assert found == [sidecar]


def test_enumerate_sidecars_ignores_legacy_config_json_suffix(
    tmp_path: Path,
) -> None:
    """Legacy ``*.config.json`` files (Phase-4 / Group-A era) are not
    surfaced — PingPair stopped writing that suffix on 2026-05-16 and
    we no longer try to discover it. Users who still need to view such
    a file can drag it in via the Analysis tab's ``Add file…`` button."""
    sub = tmp_path / "Ping_2026-05-15_2100"
    sub.mkdir()
    _write(sub, "Ping_2026-05-15_2100.config.json", _v3_sidecar("legacy"))
    _write(tmp_path, "Ping_2026-05-09_1404.config.json", _v3_sidecar("flat"))
    assert enumerate_sidecars(tmp_path) == []


# ---------------------------------------------------------------------------
# Batch loader
# ---------------------------------------------------------------------------


def test_load_many_collects_errors_via_callback(tmp_path: Path) -> None:
    """``load_many`` returns the successfully-loaded runs and routes
    every failure to the ``on_error`` callback so the Analysis view
    can show a per-file warning rather than aborting the whole scan.
    """
    good = _write(tmp_path, "good.json", _v3_sidecar("good"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    errors: list[tuple[Path, SidecarParseError]] = []
    runs = load_many([good, bad], on_error=lambda p, e: errors.append((p, e)))

    assert len(runs) == 1
    assert runs[0].run_id == "good"
    assert len(errors) == 1
    assert errors[0][0] == bad
