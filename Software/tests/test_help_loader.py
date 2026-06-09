"""Sandbox-tier tests for the Qt-free Help guide loader (Feature 8).

No QApplication — pure filesystem enumeration + title parsing.
"""

from __future__ import annotations

from pathlib import Path

from pingpair.help_loader import (
    HelpSection,
    list_sections,
    parse_title,
)


def _make_section(root: Path, slug: str, *, html: str | None = None) -> Path:
    """Create ``root/<slug>/index.html`` with ``html`` (or a default)."""
    folder = root / slug
    folder.mkdir(parents=True)
    if html is not None:
        (folder / "index.html").write_text(html, encoding="utf-8")
    return folder


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_sections_ordered_by_numeric_prefix(tmp_path: Path) -> None:
    # Created out of order on disk; result must be 1, 2, 3.
    _make_section(tmp_path, "03-run", html="<title>Run</title>")
    _make_section(tmp_path, "01-setup", html="<title>Setup</title>")
    _make_section(tmp_path, "02-config", html="<title>Config</title>")

    sections = list_sections(tmp_path)

    assert [s.order for s in sections] == [1, 2, 3]
    assert [s.slug for s in sections] == ["01-setup", "02-config", "03-run"]


def test_numeric_prefix_sorts_numerically_not_lexically(tmp_path: Path) -> None:
    # "10-..." must come AFTER "2-...", not before (lexical would flip them).
    _make_section(tmp_path, "10-tenth", html="<title>Tenth</title>")
    _make_section(tmp_path, "2-second", html="<title>Second</title>")

    sections = list_sections(tmp_path)

    assert [s.order for s in sections] == [2, 10]


# --------------------------------------------------------------------------
# Title parsing
# --------------------------------------------------------------------------


def test_title_comes_from_title_tag(tmp_path: Path) -> None:
    _make_section(tmp_path, "01-setup", html="<title>Setup</title>")
    (section,) = list_sections(tmp_path)
    assert section.title == "Setup"


def test_title_decodes_html_entities(tmp_path: Path) -> None:
    # The sidebar shows the title as plain text, so a raw entity would leak
    # through verbatim — the loader must decode it (Feature 8 / VM review).
    _make_section(tmp_path, "01-setup", html="<title>Save Options &amp; reports</title>")
    (section,) = list_sections(tmp_path)
    assert section.title == "Save Options & reports"


def test_section_key_strips_numeric_prefix(tmp_path: Path) -> None:
    # The cross-link key is the slug minus its NN- prefix, so in-guide
    # `help:<key>` links survive a renumber.
    _make_section(tmp_path, "08-troubleshooting", html="<title>Troubleshooting</title>")
    _make_section(tmp_path, "10-iperf3-reference", html="<title>iperf3 reference</title>")
    sections = {s.key: s for s in list_sections(tmp_path)}
    assert "troubleshooting" in sections
    assert "iperf3-reference" in sections


def test_title_whitespace_is_collapsed(tmp_path: Path) -> None:
    _make_section(
        tmp_path,
        "01-setup",
        html="<title>\n   Running   a\n   sweep   </title>",
    )
    (section,) = list_sections(tmp_path)
    assert section.title == "Running a sweep"


def test_title_falls_back_to_slug_when_absent(tmp_path: Path) -> None:
    _make_section(tmp_path, "04-save-options", html="<h1>No title element here</h1>")
    (section,) = list_sections(tmp_path)
    assert section.title == "04-save-options"


def test_empty_title_falls_back_to_slug(tmp_path: Path) -> None:
    _make_section(tmp_path, "05-analysis", html="<title>   </title>")
    (section,) = list_sections(tmp_path)
    assert section.title == "05-analysis"


def test_parse_title_helper_directly() -> None:
    assert parse_title("<title>X</title>", "fallback") == "X"
    assert parse_title("<TITLE>Y</TITLE>", "fallback") == "Y"  # case-insensitive
    assert parse_title("<p>no title</p>", "fallback") == "fallback"


# --------------------------------------------------------------------------
# Tolerance / filtering
# --------------------------------------------------------------------------


def test_missing_help_dir_returns_empty(tmp_path: Path) -> None:
    assert list_sections(tmp_path / "does-not-exist") == []


def test_empty_help_dir_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "help").mkdir()
    assert list_sections(tmp_path / "help") == []


def test_non_numeric_folders_are_ignored(tmp_path: Path) -> None:
    _make_section(tmp_path, "01-setup", html="<title>Setup</title>")
    _make_section(tmp_path, "notes", html="<title>Scratch</title>")
    _make_section(tmp_path, "draft-ideas", html="<title>Draft</title>")

    sections = list_sections(tmp_path)

    assert [s.slug for s in sections] == ["01-setup"]


def test_folder_without_index_is_skipped(tmp_path: Path) -> None:
    # A correctly-named folder but with no index.html must not appear.
    (tmp_path / "02-config").mkdir()
    _make_section(tmp_path, "01-setup", html="<title>Setup</title>")

    sections = list_sections(tmp_path)

    assert [s.slug for s in sections] == ["01-setup"]


def test_loose_files_in_help_dir_are_ignored(tmp_path: Path) -> None:
    # A stray file (not a directory) at the help root must not crash/leak.
    (tmp_path / "README.md").write_text("# notes", encoding="utf-8")
    _make_section(tmp_path, "01-setup", html="<title>Setup</title>")

    sections = list_sections(tmp_path)

    assert [s.slug for s in sections] == ["01-setup"]


# --------------------------------------------------------------------------
# Dataclass surface
# --------------------------------------------------------------------------


def test_section_directory_is_index_parent(tmp_path: Path) -> None:
    folder = _make_section(tmp_path, "01-setup", html="<title>Setup</title>")
    (section,) = list_sections(tmp_path)
    assert isinstance(section, HelpSection)
    assert section.directory == folder
    assert section.index_path == folder / "index.html"
