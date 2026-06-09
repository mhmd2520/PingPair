"""Smoke tests for the Analysis-tab comparison-report writers (#12).

Each writer takes a :class:`ComparisonReport` and produces a file in
its native format. We don't try to validate Word/PDF XML — just verify:

* the file gets written without raising,
* the right path / extension lands,
* the content includes at least one of the run labels (proves the
  data made it past serialization),
* the diff section is included only when len(runs) == 2.
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pytest

from pingpair.analysis import (
    CasePoint,
    ComparisonReport,
    FilterDescription,
    LoadedRun,
    Series,
    build_comparison_report,
)
from pingpair.reporting import _make_unique_sweep_dir, save_comparison_report


def _case(idx: int, payload: int, bw: int, *, thr: float = 50.0,
          lat: float = 2.0, loss: float = 0.0, jit: float = 0.5) -> CasePoint:
    return CasePoint(
        case_idx=idx,
        payload_bytes=payload,
        bandwidth_mbps_pushed=bw,
        status="ok",
        throughput_mbps_received=thr,
        jitter_ms=jit,
        packet_loss_pct=loss,
        avg_latency_ms=lat,
        min_latency_ms=lat - 0.5,
        max_latency_ms=lat + 0.5,
    )


def _run(label: str, started: datetime, thr_base: float) -> LoadedRun:
    cases = [
        _case(1, 200, 10, thr=thr_base),
        _case(2, 200, 30, thr=thr_base * 2),
        _case(3, 200, 50, thr=thr_base * 3),
    ]
    return LoadedRun(
        path=Path(f"/tmp/{label}.config.json"),
        run_id=label,
        display_label=label,
        schema_version=3,
        started_at=started,
        duration_s=950.0,
        server_ip="192.168.1.1",
        client_ip="192.168.1.2",
        protocol="udp",
        is_multi_segment=False,
        metadata={"technician": "MK"},
        series=[Series(label=label, cases=cases)],
    )


def _default_filter_desc() -> FilterDescription:
    return FilterDescription(
        case_lo=1, case_hi=20,
        payloads=(), bandwidths=(), metadata=(),
        is_default=True,
    )


def _two_run_report() -> ComparisonReport:
    a = _run("alpha", datetime(2026, 5, 9, 14, 0, 0), 10.0)
    b = _run("bravo", datetime(2026, 5, 10, 14, 0, 0), 12.0)
    # "newest-first" — bravo first, alpha second (matches Analysis tab order).
    return build_comparison_report(
        runs=[b, a],
        case_filter=lambda c: True,
        filter_description=_default_filter_desc(),
    )


def _three_run_report() -> ComparisonReport:
    a = _run("alpha", datetime(2026, 5, 9, 14, 0, 0), 10.0)
    b = _run("bravo", datetime(2026, 5, 10, 14, 0, 0), 12.0)
    c = _run("charlie", datetime(2026, 5, 11, 14, 0, 0), 14.0)
    return build_comparison_report(
        runs=[c, b, a],
        case_filter=lambda c: True,
        filter_description=_default_filter_desc(),
    )


# ---------------------------------------------------------------------------
# ComparisonReport math
# ---------------------------------------------------------------------------


def test_build_comparison_report_two_runs_has_diff() -> None:
    report = _two_run_report()
    assert report.run_count == 2
    assert report.has_diff_section
    assert len(report.per_case_diff_rows) == 3


def test_build_comparison_report_diff_orders_older_as_a() -> None:
    """Lock the A=older / B=newer / Δ=B−A column direction.

    Regression guard for the inverted-A/B bug: the runs are passed
    newest-first (``[bravo, alpha]``), but ``build_comparison_report``
    must sort by ``started_at`` so the OLDER run (alpha, thr 10.0) lands
    under column A and the NEWER (bravo, thr 12.0) under column B, with
    a positive delta. The previous code passed ``(newer, older)`` to
    ``per_case_diff``, which flipped both the columns and the sign.
    """
    report = _two_run_report()
    row = next(r for r in report.per_case_diff_rows if r.case_idx == 1)
    assert row.a_value["thr"] == 10.0  # alpha = older
    assert row.b_value["thr"] == 12.0  # bravo = newer
    assert row.delta["thr"] == 2.0  # B − A, positive


def test_build_comparison_report_three_runs_no_diff() -> None:
    report = _three_run_report()
    assert report.run_count == 3
    assert not report.has_diff_section
    assert report.per_case_diff_rows == []


def test_build_comparison_report_filter_applies() -> None:
    a = _run("alpha", datetime(2026, 5, 9, 14, 0, 0), 10.0)
    b = _run("bravo", datetime(2026, 5, 10, 14, 0, 0), 12.0)
    # Keep only case 1.
    report = build_comparison_report(
        runs=[b, a],
        case_filter=lambda c: c.case_idx == 1,
        filter_description=_default_filter_desc(),
    )
    assert report.per_run_stats[0].filtered_cases == 1
    assert len(report.per_case_diff_rows) == 1


# ---------------------------------------------------------------------------
# Writers — smoke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt,ext", [
    ("txt", ".txt"),
    ("docx", ".docx"),
    ("xlsx", ".xlsx"),
    ("pdf", ".pdf"),
])
def test_save_comparison_report_writes_file(
    tmp_path: Path, fmt: str, ext: str,
) -> None:
    report = _two_run_report()
    written = save_comparison_report(
        report, tmp_path, "compare_test", [fmt],
    )
    assert len(written) == 1
    assert written[0].suffix == ext
    assert written[0].exists()
    assert written[0].stat().st_size > 0


def test_save_comparison_report_all_formats_at_once(tmp_path: Path) -> None:
    report = _two_run_report()
    written = save_comparison_report(
        report, tmp_path, "compare_all",
        ["docx", "xlsx", "pdf", "txt"],
    )
    assert {p.suffix for p in written} == {".docx", ".xlsx", ".pdf", ".txt"}
    for p in written:
        assert p.parent.name == "compare_all"


def test_comparison_reports_embed_logo(tmp_path: Path) -> None:
    """Both Analysis-tab comparison writers embed the header logo: the .docx
    carries exactly one inline image and the .pdf an image XObject. The report
    has no charts attached, so the logo is the only embedded image."""
    from docx import Document

    from pingpair.reporting.comparison_docx import write_comparison_docx
    from pingpair.reporting.comparison_pdf import write_comparison_pdf

    report = _two_run_report()  # no chart_pngs attached
    docx_path = tmp_path / "cmp.docx"
    pdf_path = tmp_path / "cmp.pdf"
    write_comparison_docx(report, docx_path)
    write_comparison_pdf(report, pdf_path)

    assert len(Document(str(docx_path)).inline_shapes) == 1, "logo missing from comparison .docx"
    pdf = pdf_path.read_bytes()
    assert pdf[:5] == b"%PDF-"
    assert b"/XObject" in pdf, "logo image XObject missing from comparison .pdf"


# A real, decodable wide-ish PNG so every writer — including reportlab's PDF
# Image, which validates lazily at build time and rejects a flowable taller
# than the page frame — can embed it.
def _stub_png(width: int = 160, height: int = 84) -> bytes:
    from PIL import Image as _PILImage

    buf = io.BytesIO()
    _PILImage.new("RGB", (width, height), (30, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


_STUB_PNG = _stub_png()


def test_save_comparison_report_single_folder_with_analysis_images(
    tmp_path: Path,
) -> None:
    """Charts land in ``<basename>/Analysis_Images/`` and the reports at the
    top level of the SAME folder — one folder, like a normal sweep.

    Regression guard for the old two-folder split (#export-2026-06-07):
    the GUI used to create ``<basename>/`` for the charts, so this writer
    bumped to ``<basename>_2/`` for the reports, leaving two siblings.
    """
    report = _two_run_report()
    # Pre-rasterised chart PNGs in a scratch dir, exactly as the GUI hands
    # them to save_comparison_report.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pngs: dict[str, Path] = {}
    for code in ("thr", "lat", "loss", "jit"):
        p = scratch / f"{code}.png"
        p.write_bytes(_STUB_PNG)
        pngs[code] = p
    report.chart_pngs = pngs

    written = save_comparison_report(
        report, tmp_path, "Analysis_Report",
        ["docx", "xlsx", "pdf", "txt"],
    )

    folder = tmp_path / "Analysis_Report"
    # Exactly one report folder — no "_2" sibling.
    siblings = sorted(
        p.name for p in tmp_path.iterdir()
        if p.is_dir() and p.name.startswith("Analysis_Report")
    )
    assert siblings == ["Analysis_Report"]
    # Report files at the top level of that single folder.
    for ext in (".docx", ".xlsx", ".pdf", ".txt"):
        assert (folder / f"Analysis_Report{ext}").exists()
    # Charts relocated into the Analysis_Images/ subfolder, named <code>.png.
    images = folder / "Analysis_Images"
    assert images.is_dir()
    for code in ("thr", "lat", "loss", "jit"):
        assert (images / f"{code}.png").exists()
    # report.chart_pngs now points into the final folder, and every PNG is
    # part of the returned manifest.
    assert all(p.parent == images for p in report.chart_pngs.values())
    assert {p for p in written if p.suffix == ".png"} == {
        images / f"{code}.png" for code in ("thr", "lat", "loss", "jit")
    }


def test_comparison_txt_contains_run_labels(tmp_path: Path) -> None:
    report = _two_run_report()
    save_comparison_report(report, tmp_path, "labels", ["txt"])
    text = (tmp_path / "labels" / "labels.txt").read_text(encoding="utf-8")
    assert "alpha" in text
    assert "bravo" in text


def test_comparison_txt_no_diff_section_for_three_runs(tmp_path: Path) -> None:
    report = _three_run_report()
    save_comparison_report(report, tmp_path, "no_diff", ["txt"])
    text = (tmp_path / "no_diff" / "no_diff.txt").read_text(encoding="utf-8")
    assert "Per-case delta" not in text
    assert "charlie" in text


def test_comparison_txt_diff_section_present_for_two_runs(
    tmp_path: Path,
) -> None:
    report = _two_run_report()
    save_comparison_report(report, tmp_path, "with_diff", ["txt"])
    text = (tmp_path / "with_diff" / "with_diff.txt").read_text(encoding="utf-8")
    assert "Per-case delta" in text
    # Δ symbol present in the header row.
    assert "Δ" in text


def test_save_comparison_report_unknown_format_raises(tmp_path: Path) -> None:
    report = _two_run_report()
    with pytest.raises(ValueError, match="unknown report format"):
        save_comparison_report(
            report, tmp_path, "bad", ["json"],  # type: ignore[list-item]
        )


def test_filter_description_default_skips_lines() -> None:
    fd = FilterDescription(
        case_lo=1, case_hi=20,
        payloads=(), bandwidths=(), metadata=(),
        is_default=True,
    )
    assert fd.lines() == []


def test_filter_description_non_default_lists_active_filters() -> None:
    fd = FilterDescription(
        case_lo=5, case_hi=10,
        payloads=(200, 600), bandwidths=(),
        metadata=(("technician", "MK"),),
        is_default=False,
    )
    lines = fd.lines()
    assert any("Cases: 5 to 10" in ln for ln in lines)
    assert any("Payloads (B)" in ln and "200" in ln for ln in lines)
    assert any("technician: MK" in ln for ln in lines)


# --- SEC-302: Analysis-export basename must not escape the destination -------


@pytest.mark.parametrize(
    "evil",
    [
        r"..\..\..\Windows\System32\evil",
        "../../etc/passwd",
        r"C:\Windows\Temp\escape",
        "sub/dir/name",
        "..",
    ],
)
def test_make_unique_sweep_dir_cannot_escape_dest(
    tmp_path: Path, evil: str
) -> None:
    """A traversal / absolute / multi-segment basename must NOT let the
    per-sweep folder land outside ``dest_dir``.

    The Analysis Export dialog passes a free-typed basename straight to
    ``save_comparison_report`` -> ``_make_unique_sweep_dir``, and the app runs
    elevated, so this is a real CWE-22 sink. ``_sanitize_basename`` at the
    join point reduces it to one safe component.
    """
    dest = tmp_path / "reports"
    dest.mkdir()
    sweep_dir, basename = _make_unique_sweep_dir(dest, evil)
    # Created folder is a direct child of dest — nothing escaped upward.
    assert sweep_dir.parent == dest
    assert sweep_dir.resolve().is_relative_to(dest.resolve())
    # Returned basename is a single safe path component.
    assert "/" not in basename
    assert "\\" not in basename
    assert basename not in ("", "..", ".")
