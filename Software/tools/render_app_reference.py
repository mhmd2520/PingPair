"""Render faithful, readable PNGs of every PingPair tab — the source images
for the Figma "Real App Reference" page.

Unlike :mod:`gui_review` (whose offscreen renders are deliberately font-less
"boxes" — fine for its layout/collapse checks), this tool **loads the bundled
Inter font** before rendering, so the output is text-legible and is the live
Qt app's own rendering: same Fusion style, same ``theme.py`` palette, same
font. That makes these PNGs a faithful reference for design — far closer to
the real app than a hand-built vector mockup, and regenerable after any UI
change.

Usage (from the repo, with the project venv active)::

    python Software/tools/render_app_reference.py            # -> %TEMP%/pingpair_app_reference
    python Software/tools/render_app_reference.py OUTDIR      # -> OUTDIR

Output: ``<tab>_<dark|light>.png`` (1400x880) for all 8 tabs, both themes.

Pushing the renders into Figma (the brand file's "App — Real App Reference"
page) is a manual MCP step, since it needs the Figma write API:
  1. For each target rectangle, request an upload URL via ``upload_assets``
     (with the rectangle's ``nodeId`` and ``scaleMode=FILL``).
  2. POST the PNG bytes; capture the returned ``imageHash``.
  3. ``use_figma``: set ``node.fills = [{type:"IMAGE", scaleMode:"FILL",
     imageHash}]`` (setting the fill via ``use_figma`` is what actually lands
     it — the ``upload_assets`` auto-fill did not stick in testing).
Aspect is exactly 1400:880 (1.591), so a matching frame + FILL never crops.

Role is forced to Loopback so no Server listener thread spins up; the Run tab
therefore shows the Loopback panel (Server/Client panels are separate renders).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from pingpair.app import MainWindow
from pingpair.config import load_default_config
from pingpair.context import AppContext, Role
from pingpair.paths import RESOURCES_DIR
from pingpair.theme import ThemeMode, apply_theme

_WIN_W, _WIN_H = 1400, 880


def _load_inter(app: QApplication) -> bool:
    """Register the bundled Inter TTFs and set Inter as the app font.

    Without this the offscreen platform renders text as boxes. Returns whether
    at least one face loaded.
    """
    fonts_dir = RESOURCES_DIR / "fonts"
    loaded = False
    for ttf in sorted(fonts_dir.glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(ttf)) != -1:
            loaded = True
    if loaded:
        app.setFont(QFont("Inter", 10))
    return loaded


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    positional = [a for a in argv if not a.startswith("-")]
    outdir = Path(positional[0]) if positional else (
        Path(os.environ.get("TEMP", "/tmp")) / "pingpair_app_reference"
    )
    outdir.mkdir(parents=True, exist_ok=True)

    # Set the platform before QApplication is constructed (Qt reads it then),
    # so all imports can stay at module top without an E402 dance.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv)
    if not _load_inter(app):
        print("WARNING: Inter not found — text will render as boxes.")

    for mode in (ThemeMode.DARK, ThemeMode.LIGHT):
        eff = apply_theme(app, mode)
        ctx = AppContext.create(load_default_config())
        ctx.run_state.role = Role.LOOPBACK
        win = MainWindow(ctx, loopback=True)
        win.resize(_WIN_W, _WIN_H)
        win.show()
        for _ in range(8):
            app.processEvents()
        tabs = win._tabs  # dev tool: intentional private access
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            for _ in range(6):
                app.processEvents()
            name = tabs.tabText(i).lower().replace(" ", "")
            win.grab().save(str(outdir / f"{name}_{eff}.png"), "PNG")
        win.close()
        print(f"rendered all tabs ({eff}) -> {outdir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
