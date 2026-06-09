"""Report writers — Phase 4 and onward.

Public API:

* :class:`RunReport` — flat, format-agnostic record of a sweep.
* :func:`build_run_report` — flatten a :class:`SweepResult` + AppConfig.
* :func:`save_report` — top-level dispatcher: pick formats, save them all.
* :func:`save_comparison_report` — Analysis-tab Export dispatcher.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from .run_report import (
    CaseMetrics,
    MultiSegmentRunReport,
    RunReport,
    SegmentMetrics,
    _sanitize_basename,
    build_multi_run_report,
    build_run_report,
    render_filename,
    unique_basename,
)

# Re-exported as this package's public API (used by views / tests via
# ``from ..reporting import ...``); listed here so they're explicit
# re-exports rather than "unused imports".
__all__ = [
    "CaseMetrics",
    "MultiSegmentRunReport",
    "RunReport",
    "SegmentMetrics",
    "build_multi_run_report",
    "build_run_report",
    "render_filename",
    "unique_basename",
    "ReportFormat",
    "ALL_FORMATS",
    "save_report",
    "save_comparison_report",
]

ReportFormat = Literal["docx", "pdf", "txt", "xlsx"]
ALL_FORMATS: tuple[ReportFormat, ...] = ("docx", "xlsx", "pdf", "txt")


def _make_unique_sweep_dir(dest_dir: Path, basename: str) -> tuple[Path, str]:
    """Create the per-sweep output folder; return ``(dir, basename)``.

    Creates the folder with ``exist_ok=False`` and re-derives the name on
    a clash, so the directory is claimed atomically. This closes the
    TOCTOU gap between the caller's :func:`unique_basename` check and the
    mkdir — if the slot was taken in between, the next free name is used
    instead of silently sharing (and overwriting into) an existing folder.

    The basename is reduced to a single safe path component first. The sweep
    path already pre-sanitises (via :func:`render_filename`), but the Analysis
    Export dialog passes a free-typed basename straight through — and the app
    runs **elevated**, so an unsanitised ``..\\..\\Windows\\evil`` / ``C:\\x``
    would let the export ``mkdir`` + write its files *outside* the chosen
    destination (CWE-22). Sanitising here, at the path-join sink, protects
    every caller regardless of input provenance; it's idempotent for the
    already-safe sweep names.
    """
    basename = _sanitize_basename(basename)
    for _ in range(100):
        sweep_dir = dest_dir / basename
        try:
            sweep_dir.mkdir(parents=True, exist_ok=False)
            return sweep_dir, basename
        except FileExistsError:
            basename = unique_basename(dest_dir, basename)
    # Pathological — 100 consecutive collisions. Fall back so the save
    # still happens rather than raising.
    sweep_dir = dest_dir / basename
    sweep_dir.mkdir(parents=True, exist_ok=True)
    return sweep_dir, basename


def save_report(
    report: RunReport | MultiSegmentRunReport,
    dest_dir: Path,
    basename: str,
    formats: Iterable[ReportFormat],
    *,
    also_config: bool = True,
    include_appendix: bool = True,
    include_chart_pngs: bool = True,
) -> list[Path]:
    """Save the report in every requested format. Returns the files written.

    The Analysis appendix (per-metric summary stats + per-metric
    line charts) is always embedded — every saved report is now a
    self-contained artefact. The ``include_appendix`` kwarg is kept
    only so unit tests can opt out for size assertions.
    """
    sweep_dir, basename = _make_unique_sweep_dir(dest_dir, basename)
    written: list[Path] = []
    is_multi = isinstance(report, MultiSegmentRunReport)

    for fmt in formats:
        path = sweep_dir / f"{basename}.{fmt}"
        if fmt == "docx":
            if is_multi:
                from .docx_writer import write_multi_docx
                write_multi_docx(
                    report, path, include_appendix=include_appendix,
                )
            else:
                from .docx_writer import write_docx
                write_docx(
                    report, path, include_appendix=include_appendix,
                )
        elif fmt == "pdf":
            if is_multi:
                from .pdf_writer import write_multi_pdf
                write_multi_pdf(
                    report, path, include_appendix=include_appendix,
                )
            else:
                from .pdf_writer import write_pdf
                write_pdf(
                    report, path, include_appendix=include_appendix,
                )
        elif fmt == "txt":
            if is_multi:
                from .txt_writer import write_multi_txt
                write_multi_txt(
                    report, path, include_appendix=include_appendix,
                )
            else:
                from .txt_writer import write_txt
                write_txt(
                    report, path, include_appendix=include_appendix,
                )
        elif fmt == "xlsx":
            if is_multi:
                from .xlsx_writer import write_multi_xlsx
                write_multi_xlsx(
                    report, path, include_appendix=include_appendix,
                )
            else:
                from .xlsx_writer import write_xlsx
                write_xlsx(
                    report, path, include_appendix=include_appendix,
                )
        else:
            raise ValueError(f"unknown report format: {fmt!r}")
        written.append(path)

    if also_config:
        # Sidecar extension matches the Config tab profile convention —
        # plain ``.json`` so users see one consistent shape across the
        # Reports tree.
        cfg_path = sweep_dir / f"{basename}.json"
        if is_multi:
            from .config_writer import write_multi_config
            write_multi_config(report, cfg_path)
        else:
            from .config_writer import write_config
            write_config(report, cfg_path)
        written.append(cfg_path)

    if include_chart_pngs:
        # Charts/ subfolder with per-metric + breakdown PNGs.
        # (Task N, 2026-05-12.) Failures here are best-effort —
        # the main reports already saved, so a chart-rendering hiccup
        # mustn't fail the whole save.
        try:
            from .png_charts_writer import write_png_charts
            png_paths = write_png_charts(
                report, sweep_dir, is_multi=is_multi,
            )
            written.extend(png_paths)
        except Exception:  # noqa: BLE001
            pass

    return written


def save_comparison_report(
    report,
    dest_dir: Path,
    basename: str,
    formats: Iterable[ReportFormat],
) -> list[Path]:
    """Save a ComparisonReport from the Analysis tab.

    Mirrors :func:`save_report`'s on-disk layout so an Analysis-tab export
    is indistinguishable from a normal sweep's folder: one ``<basename>/``
    folder holding ``<basename>.<fmt>`` for each requested format, plus an
    ``Analysis_Images/`` subfolder containing the overlay-chart PNGs (when
    the GUI rasterised any). The caller rasterises the live pyqtgraph
    charts to a scratch location on the GUI thread and points
    ``report.chart_pngs`` there; this function relocates them into
    ``Analysis_Images/`` *before* the writers run, so the docx/pdf/txt
    embed them from their final home and the whole export lands under a
    single folder.

    (Previously the GUI created ``<basename>/`` itself for the charts and
    this function then bumped to ``<basename>_2/`` for the reports, leaving
    two sibling folders — see the screenshots in the 2026-06-07 report.)
    """
    sweep_dir, basename = _make_unique_sweep_dir(dest_dir, basename)
    written: list[Path] = []

    # Relocate any pre-rasterised chart PNGs into the per-sweep
    # Analysis_Images/ subfolder, then re-point report.chart_pngs at the
    # final files so the writers below embed them from there. Appended to
    # ``written`` after the reports to match save_report's ordering.
    png_written: list[Path] = []
    if report.chart_pngs:
        charts_dir = sweep_dir / "Analysis_Images"
        charts_dir.mkdir(parents=True, exist_ok=True)
        relocated: dict[str, Path] = {}
        for code, src in report.chart_pngs.items():
            src_path = Path(src)
            if not src_path.exists():
                continue
            target = charts_dir / f"{code}.png"
            shutil.copyfile(src_path, target)
            relocated[code] = target
            png_written.append(target)
        report.chart_pngs = relocated

    for fmt in formats:
        path = sweep_dir / f"{basename}.{fmt}"
        if fmt == "docx":
            from .comparison_docx import write_comparison_docx
            write_comparison_docx(report, path)
        elif fmt == "xlsx":
            from .comparison_xlsx import write_comparison_xlsx
            write_comparison_xlsx(report, path)
        elif fmt == "pdf":
            from .comparison_pdf import write_comparison_pdf
            write_comparison_pdf(report, path)
        elif fmt == "txt":
            from .comparison_txt import write_comparison_txt
            write_comparison_txt(report, path)
        else:
            raise ValueError(f"unknown report format: {fmt!r}")
        written.append(path)

    written.extend(png_written)
    return written
