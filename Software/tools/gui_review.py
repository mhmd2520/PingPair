"""GUI review harness — render every PingPair tab in both themes and
flag collapsed widgets, without a display or any external MCP.

This is the project's stand-in for a "GUI review" tool: there is no
desktop-Qt review MCP (Figma reads design files, not a running Qt app),
so we render the real :class:`MainWindow` offscreen, screenshot each tab
under Light and Dark, and dump every input/button/spin/combo size so
layout regressions (e.g. a QFormLayout starving its fields to ~14px)
show up as text even when image review isn't available.

Usage (from the repo, with the project venv active)::

    python Software/tools/gui_review.py            # PNGs -> %TEMP%/pingpair_gui_review
    python Software/tools/gui_review.py OUTDIR      # PNGs -> OUTDIR
    python Software/tools/gui_review.py --geom       # also print widget geometry

Notes:
- Runs on the offscreen Qt platform, so text renders as boxes (the
  headless venv ships no fonts) — the PNGs are for *layout/colour/border*
  review; run the real app for text. The ``--geom`` dump is font-free
  and is the reliable signal for collapse/sizing bugs.
- Role is forced to Loopback so no Server listener thread spins up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Widget labels may carry non-cp1252 glyphs (e.g. arrows); keep the Windows
# console from aborting the whole dump on an un-encodable character.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLineEdit, QPushButton, QAbstractSpinBox, QComboBox,
)

from pingpair.app import MainWindow  # noqa: E402
from pingpair.config import load_default_config  # noqa: E402
from pingpair.context import AppContext, Role  # noqa: E402
from pingpair.theme import ThemeMode, apply_theme  # noqa: E402

_COLLAPSE_H = 16
_COLLAPSE_W = 12


def _dump_geometry(win, theme: str) -> int:
    """Print input/button/spin/combo sizes; return the collapsed count."""
    tabs = win._tabs  # noqa: SLF001 - dev tool, intentional
    collapsed = 0
    for i in range(tabs.count()):
        w = tabs.widget(i)
        tabs.setCurrentIndex(i)
        for _ in range(4):
            QApplication.processEvents()
        print(f"--- [{theme}] {tabs.tabText(i)} ---")
        for cls, lbl in ((QLineEdit, "edit"), (QPushButton, "btn"),
                         (QAbstractSpinBox, "spin"), (QComboBox, "combo")):
            for c in w.findChildren(cls):
                if not c.isVisible():
                    continue
                s = c.size()
                bad = s.height() < _COLLAPSE_H or s.width() < _COLLAPSE_W
                if bad:
                    collapsed += 1
                txt = (c.text()[:18] if hasattr(c, "text") else "")
                tag = "  <-- COLLAPSED" if bad else ""
                print(f"  {lbl:5} {s.width():4}x{s.height():3} '{txt}'{tag}")
    return collapsed


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    geom = "--geom" in argv
    positional = [a for a in argv if not a.startswith("-")]
    outdir = Path(positional[0]) if positional else (
        Path(os.environ.get("TEMP", "/tmp")) / "pingpair_gui_review"
    )
    outdir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    total_collapsed = 0
    for mode in (ThemeMode.DARK, ThemeMode.LIGHT):
        eff = apply_theme(app, mode)
        ctx = AppContext.create(load_default_config())
        ctx.run_state.role = Role.LOOPBACK
        win = MainWindow(ctx, loopback=True)
        win.resize(1320, 820)
        win.show()
        for _ in range(6):
            app.processEvents()
        tabs = win._tabs  # noqa: SLF001
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            for _ in range(4):
                app.processEvents()
            name = tabs.tabText(i).lower().replace(" ", "")
            pix = win.grab()
            if pix.width() > 1150 or pix.height() > 1150:
                pix = pix.scaled(1150, 1150, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            pix.save(str(outdir / f"{name}_{eff}.png"), "PNG")
        if geom:
            total_collapsed += _dump_geometry(win, eff)
        win.close()
        print(f"rendered {eff} -> {outdir}")

    if geom:
        print(f"\nCollapsed widgets found: {total_collapsed}")
        return 1 if total_collapsed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
