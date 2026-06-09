"""Group B — case-subset picker tests.

Covers four pieces of the feature that don't need a live network:

1. ``ControlClient.run_sweep(selected_indexes=...)`` only walks the
   requested cases.
2. The QSettings round-trip preserves and restores the selection.
3. ``build_run_report(selected_case_indexes=...)`` carries the field
   through, and the config.json sidecar serialises it under the new
   schema_version=3.
4. The ``ProtoConst`` (full-list) optimisation: passing all 20 indexes
   is equivalent to passing ``None``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from pingpair.config import load_default_config
from pingpair.core.case import CaseResult
from pingpair.core.control import server as server_module
from pingpair.core.control.client import (
    ControlClient,
    SweepCaseEntry,
    SweepResult,
)
from pingpair.core.control.protocol import FramedSocket, Message
from pingpair.core.control.server import ControlServer
from pingpair.core.parsers.fping import FpingResult
from pingpair.core.parsers.iperf3 import IperfResult
from pingpair.core.plan import TestCase, build_plan
from pingpair.core.runner import RunResult
from pingpair.reporting import build_run_report, save_report


# ---------------------------------------------------------------------------
# Shared fakes (mirrors test_control_loopback.py / test_reports.py shapes)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeRunResult:
    def __init__(self, rc: int = 0) -> None:
        self.returncode = rc
        self.stdout = '{"fake": true}'
        self.stderr = ""


class _FakeProcRunner:
    def __init__(self, *_a, **_kw) -> None:
        pass

    def start(self) -> None:
        pass

    def wait(self, timeout_s: float | None = None) -> _FakeRunResult:
        return _FakeRunResult()

    def stop(self, *_a, **_kw) -> None:
        pass


class _FakeCaseRunner:
    """Stand-in for CaseRunner that doesn't spawn iperf3 / fping.

    Returns a CaseResult with realistic-looking metrics so SweepCaseEntry.ok
    evaluates True. Mirrors the shape used in test_reports.py.
    """

    def __init__(self, cfg, case: TestCase, *, loopback=False, on_line=None) -> None:
        self.case = case

    def run(self) -> CaseResult:
        from pingpair.core.runner import ProcSpec

        def _run_result(name: str) -> RunResult:
            return RunResult(
                spec=ProcSpec(name=name, argv=[name], cwd=Path(".")),
                returncode=0, stdout="", stderr="",
                started_at=0.0, ended_at=1.0,
            )

        iperf = IperfResult(
            throughput_mbps=float(self.case.bandwidth_mbps),
            jitter_ms=0.05,
            packet_loss_pct=0.0,
            raw={"end": {"sum": {}}},
        )
        fp = FpingResult(
            target="127.0.0.1",
            sent=300, received=300, loss_pct=0.0,
            min_ms=0.10, avg_ms=0.40, max_ms=2.10,
            elapsed_s=1.0,
        )
        return CaseResult(
            case=self.case,
            iperf_client=iperf,
            iperf_intervals=[],
            iperf_server_raw="",
            fping=fp,
            iperf_client_run=_run_result("iperf3-client"),
            fping_run=_run_result("fping"),
            iperf_server_run=_run_result("iperf3-server"),
            error=None,
        )

    def stop(self) -> None:
        pass


@contextmanager
def _server_on(port: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[ControlServer]:
    monkeypatch.setattr(server_module, "ProcRunner", _FakeProcRunner)
    cfg = load_default_config()
    cfg.network.control_port = port  # type: ignore[assignment]

    srv = ControlServer(cfg)
    thread = threading.Thread(
        target=srv.serve_forever,
        kwargs={"bind_host": "127.0.0.1"},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        srv.stop()
        thread.join(timeout=2.0)
        pytest.fail(f"Server failed to bind 127.0.0.1:{port}")
    try:
        yield srv
    finally:
        srv.stop()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 1. ControlClient subset filter
# ---------------------------------------------------------------------------


def test_run_sweep_with_subset_only_walks_requested_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking for cases 1, 5, and 11 produces exactly those three entries."""
    port = _free_port()
    monkeypatch.setattr(
        "pingpair.core.control.client.CaseRunner", _FakeCaseRunner
    )

    with _server_on(port, monkeypatch):
        cfg = load_default_config()
        cfg.network.control_port = port  # type: ignore[assignment]
        client = ControlClient(cfg)
        sweep = client.run_sweep(
            server_host="127.0.0.1",
            selected_indexes=[1, 5, 11],
        )

    assert len(sweep.cases) == 3
    assert [e.case.index for e in sweep.cases] == [1, 5, 11]
    # Sanity-check that every chosen case actually got a CaseResult attached.
    assert all(e.case_result is not None for e in sweep.cases)


def test_run_sweep_subset_none_runs_all_20(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """selected_indexes=None falls back to the canonical 20-case sweep."""
    port = _free_port()
    monkeypatch.setattr(
        "pingpair.core.control.client.CaseRunner", _FakeCaseRunner
    )

    with _server_on(port, monkeypatch):
        cfg = load_default_config()
        cfg.network.control_port = port  # type: ignore[assignment]
        client = ControlClient(cfg)
        sweep = client.run_sweep(server_host="127.0.0.1")

    assert len(sweep.cases) == 20
    assert [e.case.index for e in sweep.cases] == list(range(1, 21))


def test_run_sweep_subset_empty_list_runs_all_20(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty subset list is treated the same as None (all 20)."""
    port = _free_port()
    monkeypatch.setattr(
        "pingpair.core.control.client.CaseRunner", _FakeCaseRunner
    )

    with _server_on(port, monkeypatch):
        cfg = load_default_config()
        cfg.network.control_port = port  # type: ignore[assignment]
        client = ControlClient(cfg)
        sweep = client.run_sweep(server_host="127.0.0.1", selected_indexes=[])

    assert len(sweep.cases) == 20


def test_run_sweep_subset_with_unknown_index_skips_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale persisted indexes (e.g. 99) are filtered out, not an error."""
    port = _free_port()
    monkeypatch.setattr(
        "pingpair.core.control.client.CaseRunner", _FakeCaseRunner
    )

    with _server_on(port, monkeypatch):
        cfg = load_default_config()
        cfg.network.control_port = port  # type: ignore[assignment]
        client = ControlClient(cfg)
        sweep = client.run_sweep(
            server_host="127.0.0.1",
            selected_indexes=[1, 99, 7],
        )

    # Only the two valid indexes survive the filter.
    assert [e.case.index for e in sweep.cases] == [1, 7]


# ---------------------------------------------------------------------------
# 2. QSettings round-trip
# ---------------------------------------------------------------------------


def test_settings_roundtrip_selected_case_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """save_from(rs) → load_into(rs2) preserves the subset.

    QSettings backs onto an INI file when we override the format - much
    nicer for unit tests than poking the live registry.
    """
    from PySide6.QtCore import QSettings, QCoreApplication
    # Make sure a QApplication exists (QSettings in INI mode still wants one).
    QCoreApplication.instance() or QCoreApplication([])

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path),
    )

    from pingpair.context import RunState
    from pingpair import settings

    cfg = load_default_config()
    rs1 = RunState.from_config(cfg)
    rs1.selected_case_indexes = [1, 3, 5, 7, 11]
    settings.save_from(rs1)

    rs2 = RunState.from_config(cfg)
    settings.load_into(rs2)
    assert rs2.selected_case_indexes == [1, 3, 5, 7, 11]


def test_settings_roundtrip_empty_means_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list persists as an empty string and reloads as []."""
    from PySide6.QtCore import QSettings, QCoreApplication
    QCoreApplication.instance() or QCoreApplication([])

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path),
    )

    from pingpair.context import RunState
    from pingpair import settings

    cfg = load_default_config()
    rs1 = RunState.from_config(cfg)
    rs1.selected_case_indexes = []
    settings.save_from(rs1)

    rs2 = RunState.from_config(cfg)
    settings.load_into(rs2)
    assert rs2.selected_case_indexes == []


def test_wifi_offline_adapter_marker_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash-recovery Wi-Fi marker persists and clears via None."""
    from PySide6.QtCore import QSettings, QCoreApplication
    QCoreApplication.instance() or QCoreApplication([])

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path),
    )

    from pingpair import settings

    assert settings.load_wifi_offline_adapter() is None
    settings.save_wifi_offline_adapter("Wi-Fi")
    assert settings.load_wifi_offline_adapter() == "Wi-Fi"
    settings.save_wifi_offline_adapter(None)
    assert settings.load_wifi_offline_adapter() is None


def test_settings_parse_int_list_handles_garbage() -> None:
    """Stray whitespace / non-integer tokens are silently dropped.

    Acceptance behaviour we want to lock in:
      - 1            → 1   (int)
      - "2"          → 2   (int(str) succeeds)
      - 3.0          → 3   (int(float) truncates)
      - None         → dropped (TypeError caught)
      - "x" / "abc"  → dropped (ValueError caught)
      - "" / spaces  → dropped after strip()
    """
    from pingpair.settings import _parse_int_list
    assert _parse_int_list("1, 2 ,3,,abc,7") == [1, 2, 3, 7]
    assert _parse_int_list([1, "2", 3.0, None, "x"]) == [1, 2, 3]
    # Duplicates are de-duped, ordering preserved.
    assert _parse_int_list("3,1,3,2,1") == [3, 1, 2]
    # Non-string non-list types yield empty.
    assert _parse_int_list(None) == []
    assert _parse_int_list(42) == []


# ---------------------------------------------------------------------------
# 3. RunReport + sidecar serialisation
# ---------------------------------------------------------------------------


def _mini_sweep() -> SweepResult:
    """Three-case sweep mirroring test_reports.py's helper, kept local
    so the subset test file is self-contained."""
    cases = [
        TestCase(index=2, payload_bytes=200, bandwidth_mbps=30, duration_s=30),
        TestCase(index=8, payload_bytes=600, bandwidth_mbps=70, duration_s=30),
    ]

    def _rr(name: str) -> RunResult:
        from pingpair.core.runner import ProcSpec
        return RunResult(
            spec=ProcSpec(name=name, argv=[name], cwd=Path(".")),
            returncode=0, stdout="", stderr="",
            started_at=0.0, ended_at=1.0,
        )

    entries = []
    for c in cases:
        cr = CaseResult(
            case=c,
            iperf_client=IperfResult(
                throughput_mbps=float(c.bandwidth_mbps),
                jitter_ms=0.05, packet_loss_pct=0.0, raw={},
            ),
            iperf_intervals=[],
            iperf_server_raw="",
            fping=FpingResult(
                target="x", sent=1, received=1, loss_pct=0.0,
                min_ms=0.1, avg_ms=0.2, max_ms=0.3, elapsed_s=1.0,
            ),
            iperf_client_run=_rr("iperf3-client"),
            fping_run=_rr("fping"),
            iperf_server_run=_rr("iperf3-server"),
            error=None,
        )
        entries.append(SweepCaseEntry(
            case=c, case_result=cr, server_iperf3_json="", server_returncode=0,
        ))
    return SweepResult(
        started_at=time.time() - 60, ended_at=time.time(), cases=entries
    )


def test_build_run_report_carries_selected_indexes() -> None:
    cfg = load_default_config()
    report = build_run_report(
        _mini_sweep(),
        cfg,
        run_id="SUBSET_RUN",
        selected_case_indexes=[2, 8],
    )
    assert report.selected_case_indexes == [2, 8]
    assert report.cases_total == 2


def test_config_json_sidecar_records_selection(tmp_path: Path) -> None:
    cfg = load_default_config()
    report = build_run_report(
        _mini_sweep(),
        cfg,
        run_id="SUBSET_RUN",
        selected_case_indexes=[2, 8],
    )
    save_report(
        report, tmp_path, "SUBSET_RUN", formats=[], also_config=True
    )
    # Q2 (2026-05-16): sidecar extension unified to plain ``.json``;
    # Q1 also bumped schema_version 3 -> 5 with additive gateway +
    # nic_override keys (both default to None for unmodified profiles).
    sidecar = tmp_path / "SUBSET_RUN" / "SUBSET_RUN.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema_version"] == 5
    assert data["selected_case_indexes"] == [2, 8]
    # Back-compat: cases_total still reflects what actually ran.
    assert data["cases_total"] == 2


def test_config_json_sidecar_full_run_has_empty_selection(tmp_path: Path) -> None:
    """A full 20-case run records ``selected_case_indexes=[]`` so a
    consumer can tell 'user picked everything' from 'user picked a subset'."""
    cfg = load_default_config()
    report = build_run_report(
        _mini_sweep(),
        cfg,
        run_id="FULL_RUN",
        # No selected_case_indexes kwarg → empty list.
    )
    save_report(report, tmp_path, "FULL_RUN", formats=[], also_config=True)
    data = json.loads(
        # Q2: sidecar extension is now plain .json.
        (tmp_path / "FULL_RUN" / "FULL_RUN.json").read_text("utf-8")
    )
    assert data["selected_case_indexes"] == []


# ---------------------------------------------------------------------------
# 4. Plan filter sanity (cheap unit-only test, no network)
# ---------------------------------------------------------------------------


def test_build_plan_then_filter_preserves_order() -> None:
    """Whatever subset we filter, the order matches build_plan()
    (payload-major: outer = payload, inner = bandwidth).
    """
    from pingpair.core.plan import build_plan

    cfg = load_default_config()
    plan = build_plan(cfg)
    # Canonical 20 cases by index.
    assert [c.index for c in plan] == list(range(1, 21))
    # First five cases share payload=200 (bandwidth varies), next five
    # share payload=600, etc. Encodes the payload-major ordering rule.
    assert all(c.payload_bytes == 200 for c in plan[0:5])
    assert all(c.payload_bytes == 600 for c in plan[5:10])
    assert all(c.payload_bytes == 1000 for c in plan[10:15])
    assert all(c.payload_bytes == 1300 for c in plan[15:20])
    # Filtering preserves the relative order (the filter is just a set
    # membership test against case.index — no resorting).
    requested = {3, 1, 11, 7}  # deliberately out-of-order input
    filtered = [c for c in plan if c.index in requested]
    assert [c.index for c in filtered] == [1, 3, 7, 11]
