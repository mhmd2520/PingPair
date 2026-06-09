"""Discover the folder-based Help guide sections.

Intentionally **Qt-free** so the enumeration logic unit-tests in the sandbox
tier without spawning a ``QApplication``. The view layer
(:mod:`pingpair.views.help_view`) calls :func:`list_sections` to populate its
sidebar, then renders each section's ``index.html`` in a ``QTextBrowser`` with
theme CSS injected at render time.

Layout walked::

    resources/help/
        01-setup/index.html
        02-config/index.html
        ...

The ``NN`` numeric prefix on each folder name drives ordering; each file's
``<title>`` drives the sidebar label (falling back to the folder slug when the
``<title>`` is missing). Folders without a numeric prefix or without an
``index.html`` are ignored, so stray notes/scratch folders never leak into the
guide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Folder name shape: leading digits, optional '-' / '_' separator, then a slug.
_PREFIX_RE = re.compile(r"^(\d+)")
# The same leading-number prefix, consumed (separator included) to yield the
# stable cross-link key — e.g. "08-troubleshooting" -> "troubleshooting".
_KEY_RE = re.compile(r"^\d+[-_]?")
# First <title>…</title> anywhere in the file (DOTALL so it can span lines).
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

INDEX_FILENAME = "index.html"

# Defensive cap on how much of an index file we read just to find the
# <title> — the title lives in the <head>, so a few KiB is ample and we
# never slurp a pathologically large file into memory.
_TITLE_SCAN_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class HelpSection:
    """One navigable guide section."""

    slug: str          # folder name, e.g. "01-setup"
    order: int         # numeric prefix, e.g. 1
    title: str         # from <title>, or the slug when absent
    index_path: Path   # absolute path to the section's index.html

    @property
    def directory(self) -> Path:
        """The section folder — used as the image search path at render time."""
        return self.index_path.parent

    @property
    def key(self) -> str:
        """Cross-link target: the slug minus its ``NN-`` prefix.

        Renumber-stable — ``"08-troubleshooting"`` and a later
        ``"09-troubleshooting"`` both resolve to ``"troubleshooting"`` — so an
        in-guide ``<a href="help:troubleshooting">`` keeps working when sections
        are reordered.
        """
        return _KEY_RE.sub("", self.slug)


def _parse_order(slug: str) -> int | None:
    """Return the leading integer of ``slug`` (e.g. ``"03-run"`` -> 3), or None."""
    m = _PREFIX_RE.match(slug)
    return int(m.group(1)) if m else None


def parse_title(html: str, fallback: str) -> str:
    """Extract the first ``<title>`` text, collapsing whitespace.

    HTML entities are decoded (``&amp;`` -> ``&``) so the sidebar — which shows
    the title as *plain* text — never leaks a raw entity. Falls back to
    ``fallback`` (the slug) when there's no ``<title>`` or it's empty. Kept
    module-public so the view + tests share one definition.
    """
    from html import unescape

    m = _TITLE_RE.search(html)
    if not m:
        return fallback
    title = unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    return title or fallback


def list_sections(help_dir: Path) -> list[HelpSection]:
    """Enumerate guide sections under ``help_dir``, ordered by ``NN`` prefix.

    Tolerant by design: a missing/empty ``help_dir`` returns ``[]``; folders
    without a numeric prefix or without an ``index.html`` are skipped; an
    unreadable index file is skipped rather than raising. Ties on the numeric
    prefix break by slug so ordering is always deterministic.
    """
    try:
        if not help_dir.is_dir():
            return []
        entries = sorted(help_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    sections: list[HelpSection] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        slug = entry.name
        order = _parse_order(slug)
        if order is None:
            continue  # not an NN-prefixed section folder
        index_path = entry / INDEX_FILENAME
        if not index_path.is_file():
            continue
        try:
            with index_path.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(_TITLE_SCAN_BYTES)
        except OSError:
            continue
        sections.append(
            HelpSection(
                slug=slug,
                order=order,
                title=parse_title(head, slug),
                index_path=index_path,
            )
        )

    sections.sort(key=lambda s: (s.order, s.slug))
    return sections
