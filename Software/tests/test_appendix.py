"""Smoke tests for the auto-bundle analysis appendix (#13).

Covers:

* run_to_loaded round-trip preserves case data.
* Each per-sweep writer, when given ``include_appendix=True``, writes
  a non-empty file. The txt output gets sniffed for the appendix
  header to prove the code path actually ran.
* The :func:`save_report` dispatcher honours the flag.
* Default behaviour (no flag) is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pingpair.config import load_default_config
from pingpair.reporting import (
    build_multi_run_report,
    build_run_report,
    save_report,
)
from pingpair.analysis.run_to_loaded import (
    from_multi_segment_report,
    from_run_report,
)
from tests.test_multi_segment import _three_segments
from tests.test_reports import _fake_sweep_result


@pytest.fixture
def run_report():
    return build_run_report(
        _fake_sweep_result(),
        load_default_config(),
    )


@pytest.fixture
def multi_report():
    return build_multi_run_report(
        _three_segments(),
        load_default_config(),
    )


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------


def test_from_run_report_preserves_cases(run_report) -> None:
    loaded = from_run_report(run_report)
    assert loaded.run_id == run_report.run_id
    assert not loaded.is_multi_segment
    assert len(loaded.series) == 1
    assert len(loaded.series[0].cases) == len(run_report.cases)
    a = run_report.cases[0]
    b = loaded.series[0].cases[0]
    assert b.case_idx == a.case_idx
    assert b.throughput_mbps_received == a.throughput_mbps_received


def test_from_multi_segment_report_one_series_per_segment(
    multi_report,
) -> None:
    loaded = from_multi_segment_report(multi_report)
    assert loaded.is_multi_segment
    assert len(loaded.series) == len(multi_report.segments)
    assert loaded.series[0].label.startswith(multi_report.run_id)


# ---------------------------------------------------------------------------
# save_report with include_appendix
# ---------------------------------------------------------------------------


def test_save_report_appendix_off_no_appendix_in_txt(
    tmp_path: Path, run_report,
) -> None:
    written = save_report(
        run_report, tmp_path, "no_appendix", ["txt"],
        also_config=False, include_appendix=False, include_chart_pngs=False)
    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    assert "Analysis appendix" not in text


def test_save_report_appendix_on_adds_section_to_txt(
    tmp_path: Path, run_report,
) -> None:
    written = save_report(
        run_report, tmp_path, "with_appendix", ["txt"],
        also_config=False, include_appendix=True, include_chart_pngs=False)
    text = written[0].read_text(encoding="utf-8")
    assert "Analysis appendix" in text
    # Stats table headers should be present.
    assert "Samples" in text
    assert "Throughput" in text


def test_save_report_appendix_all_formats(
    tmp_path: Path, run_report,
) -> None:
    written = save_report(
        run_report, tmp_path, "all_fmt",
        ["docx", "xlsx", "pdf", "txt"],
        also_config=False, include_appendix=True, include_chart_pngs=False)
    by_ext = {p.suffix: p for p in written}
    assert set(by_ext.keys()) == {".docx", ".xlsx", ".pdf", ".txt"}
    for p in written:
        assert p.stat().st_size > 0
    assert "Analysis appendix" in by_ext[".txt"].read_text(encoding="utf-8")


def test_save_report_multi_segment_appendix(
    tmp_path: Path, multi_report,
) -> None:
    written = save_report(
        multi_report, tmp_path, "multi_app", ["txt"],
        also_config=False, include_appendix=True, include_chart_pngs=False)
    text = written[0].read_text(encoding="utf-8")
    assert "Analysis appendix" in text


def test_save_report_default_now_includes_appendix(
    tmp_path: Path, run_report,
) -> None:
    """#13 follow-up: appendix is unconditionally on (no toggle anymore)."""
    written = save_report(
        run_report, tmp_path, "default", ["txt"],
        also_config=False, include_chart_pngs=False)
    text = written[0].read_text(encoding="utf-8")
    assert "Analysis appendix" in text


# ---------------------------------------------------------------------------
# Renderer math
# ---------------------------------------------------------------------------


def test_appendix_stat_rows_match_metrics() -> None:
    from pingpair.analysis import METRICS
    from pingpair.analysis.sidecar_loader import (
        CasePoint, LoadedRun, Series,
    )
    from pingpair.reporting.appendix import _APPENDIX_HEADERS, _stat_rows

    cases = [
        CasePoint(
            case_idx=1, payload_bytes=200, bandwidth_mbps_pushed=10,
            status="ok",
            throughput_mbps_received=10.0,
            jitter_ms=0.1, packet_loss_pct=0.0,
            avg_latency_ms=1.0,
            min_latency_ms=0.5, max_latency_ms=1.5,
        ),
    ]
    run = LoadedRun(
        path=Path("/tmp/x"), run_id="x", display_label="x",
        schema_version=3, started_at=datetime.now(), duration_s=10.0,
        server_ip="1.1.1.1", client_ip="1.1.1.2", protocol="udp",
        is_multi_segment=False, metadata={},
        series=[Series("x", cases)],
    )
    rows = _stat_rows(run)
    assert len(rows) == len(METRICS)
    for r in rows:
        assert len(r) == len(_APPENDIX_HEADERS)


# ---------------------------------------------------------------------------
# Rich appendix breakdowns (#9 follow-up 2026-05-12)
# ---------------------------------------------------------------------------


def test_breakdown_by_payload_groups_correctly() -> None:
    from pingpair.reporting.appendix import _breakdown_by_payload
    from pingpair.analysis.sidecar_loader import (
        CasePoint, LoadedRun, Series,
    )
    cases = [
        CasePoint(case_idx=1, payload_bytes=200, bandwidth_mbps_pushed=10,
                  status="ok", throughput_mbps_received=10.0,
                  jitter_ms=0.1, packet_loss_pct=0.0, avg_latency_ms=1.0,
                  min_latency_ms=0.5, max_latency_ms=1.5),
        CasePoint(case_idx=2, payload_bytes=200, bandwidth_mbps_pushed=30,
                  status="ok", throughput_mbps_received=30.0,
                  jitter_ms=0.2, packet_loss_pct=0.0, avg_latency_ms=2.0,
                  min_latency_ms=1.5, max_latency_ms=2.5),
        CasePoint(case_idx=3, payload_bytes=600, bandwidth_mbps_pushed=10,
                  status="ok", throughput_mbps_received=8.0,
                  jitter_ms=0.3, packet_loss_pct=0.0, avg_latency_ms=3.0,
                  min_latency_ms=2.5, max_latency_ms=3.5),
    ]
    run = LoadedRun(
        path=Path("/tmp/x"), run_id="x", display_label="x",
        schema_version=3, started_at=datetime.now(), duration_s=0.0,
        server_ip="", client_ip="", protocol="udp",
        is_multi_segment=False, metadata={}, series=[Series("x", cases)],
    )
    rows = _breakdown_by_payload(run)
    # Two distinct payloads.
    assert len(rows) == 2
    # First column = bucket label.
    assert rows[0][0] == "200 B"
    assert rows[1][0] == "600 B"
    # Cases column.
    assert rows[0][1] == "2"
    assert rows[1][1] == "1"


def test_breakdown_by_bandwidth_groups_correctly() -> None:
    from pingpair.reporting.appendix import _breakdown_by_bandwidth
    from pingpair.analysis.sidecar_loader import (
        CasePoint, LoadedRun, Series,
    )
    cases = [
        CasePoint(case_idx=1, payload_bytes=200, bandwidth_mbps_pushed=10,
                  status="ok", throughput_mbps_received=10.0,
                  jitter_ms=0.1, packet_loss_pct=0.0, avg_latency_ms=1.0,
                  min_latency_ms=0.5, max_latency_ms=1.5),
        CasePoint(case_idx=2, payload_bytes=600, bandwidth_mbps_pushed=10,
                  status="ok", throughput_mbps_received=9.0,
                  jitter_ms=0.2, packet_loss_pct=0.0, avg_latency_ms=2.0,
                  min_latency_ms=1.5, max_latency_ms=2.5),
        CasePoint(case_idx=3, payload_bytes=200, bandwidth_mbps_pushed=90,
                  status="ok", throughput_mbps_received=80.0,
                  jitter_ms=0.3, packet_loss_pct=0.5, avg_latency_ms=3.0,
                  min_latency_ms=2.5, max_latency_ms=3.5),
    ]
    run = LoadedRun(
        path=Path("/tmp/x"), run_id="x", display_label="x",
        schema_version=3, started_at=datetime.now(), duration_s=0.0,
        server_ip="", client_ip="", protocol="udp",
        is_multi_segment=False, metadata={}, series=[Series("x", cases)],
    )
    rows = _breakdown_by_bandwidth(run)
    assert len(rows) == 2
    assert rows[0][0] == "10 Mbps"
    assert rows[1][0] == "90 Mbps"
    assert rows[0][1] == "2"


def test_appendix_pdf_writer_includes_charts(tmp_path, run_report) -> None:
    """Smoke: PDF appendix path runs end-to-end (matplotlib + reportlab)."""
    written = save_report(
        run_report, tmp_path, "pdf_smoke", ["pdf"],
        also_config=False, include_appendix=True, include_chart_pngs=False)
    assert written[0].exists()
    assert written[0].stat().st_size > 5000  # non-trivial PDF with charts


# ---------------------------------------------------------------------------
# Task N — Charts/ PNG subfolder export
# ---------------------------------------------------------------------------


def test_save_report_writes_charts_subfolder(
    tmp_path: Path, run_report,
) -> None:
    """include_chart_pngs=True drops a Charts/ subfolder with PNG files."""
    written = save_report(
        run_report, tmp_path, "with_charts", ["txt"],
        also_config=False, include_chart_pngs=True,
    )
    charts_dir = tmp_path / "with_charts" / "Analysis_Images"
    assert charts_dir.is_dir()
    pngs = list(charts_dir.glob("*.png"))
    # 4 metric charts + 2 breakdown charts when matplotlib is available.
    assert len(pngs) >= 1, "expected at least one PNG written"
    # Every PNG is non-trivial (matplotlib output is typically 5-30 kB).
    for p in pngs:
        assert p.stat().st_size > 500
    # The returned paths include the PNGs.
    assert any(p.suffix == ".png" for p in written)


def test_save_report_chart_pngs_off_skips_charts_folder(
    tmp_path: Path, run_report,
) -> None:
    """include_chart_pngs=False writes no Charts/ subfolder."""
    save_report(
        run_report, tmp_path, "no_charts", ["txt"],
        also_config=False, include_chart_pngs=False,
    )
    charts_dir = tmp_path / "no_charts" / "Analysis_Images"
    assert not charts_dir.exists()


def test_png_writer_unavailable_matplotlib_writes_note(
    tmp_path: Path, monkeypatch, run_report,
) -> None:
    """When matplotlib_available()=False, a charts_unavailable.txt note is written."""
    import pingpair.reporting.png_charts_writer as png_mod
    monkeypatch.setattr(
        png_mod, "matplotlib_available", lambda: False,
    )
    written = save_report(
        run_report, tmp_path, "no_mpl", ["txt"],
        also_config=False, include_chart_pngs=True,
    )
    note = tmp_path / "no_mpl" / "Analysis_Images" / "charts_unavailable.txt"
    assert note.exists()
    assert "matplotlib" in note.read_text(encoding="utf-8").lower()
