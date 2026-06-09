"""Render the first-boot Welcome tour (intro + 7 cards, both themes) — the
source images for the Figma "Welcome Tour board" on the brand file's
**Application** page.

Companion to :mod:`render_app_reference` (which renders the 8 tab frames). Like
that tool it **loads the bundled Inter font** and grabs the live Qt widget, so
the output is the app's own rendering — not a hand-built mockup. Each card's
inline figure is resolved from ``resources/help/_shots/<theme>/<role>/`` at
render time, so this always reflects the **current** ``_shots`` tree (run
``build_help_shots.py`` first if you've just dropped a new screenshot batch).
That's why the "Setup — go green" cards show whatever Setup shot
``setup/01-checks-overview`` currently points at.

Usage (from the repo, with the project venv active)::

    python Software/tools/render_welcome_reference.py            # -> %TEMP%/pingpair_welcome_reference
    python Software/tools/render_welcome_reference.py OUTDIR      # -> OUTDIR

Output: ``welcome_<dark|light>_<slot>.png`` (1480x800) where ``slot`` is
``intro`` then ``c1``..``c7`` — i.e. the intro/welcome card followed by
``QUICK_START_CARDS[0..6]`` in tour order.

Pushing the renders into Figma (the "Welcome Tour board", node 379:3 on the
Application page) is a manual MCP step, same pattern as render_app_reference:
  1. ``upload_assets`` with the target rectangle's ``nodeId`` + ``scaleMode=FILL``.
  2. POST the PNG bytes to the returned ``submitUrl`` (auto-fills the node).
The board's image rectangles (as of 2026-06-08) are, in ``intro,c1..c7`` order:
  Dark  row (380:x): 380:4, 380:7, 380:10, 380:13, 380:16, 380:19, 380:22, 380:25
  Light row (381:x): 381:4, 381:7, 381:10, 381:13, 381:16, 381:19, 381:22, 381:25
Re-enumerate with ``use_figma`` if the file structure has changed since.

The dialog ``role`` is forced to Client, but the Setup cards (#2-4) are pinned
to Server/Client/Loopback inside ``welcome_cards`` and Run/Save (#5-6) to
Client, so the role argument only matters for cards without a pinned role.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from pingpair.paths import RESOURCES_DIR
from pingpair.theme import ThemeMode, apply_theme
from pingpair.views.welcome import WelcomeDialog
from pingpair.welcome_cards import QUICK_START_CARDS

# Match the board rectangles' ~1.85 aspect so a FILL fill never crops.
_WIN_W, _WIN_H = 1480, 800


def _load_inter(app: QApplication) -> bool:
    """Register the bundled Inter TTFs and set Inter as the app font.

    Without this the offscreen platform renders text as boxes. Returns whether
    at least one face loaded.
    """
    loaded = False
    for ttf in sorted((RESOURCES_DIR / "fonts").glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(ttf)) != -1:
            loaded = True
    if loaded:
        app.setFont(QFont("Inter", 10))
    return loaded


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    positional = [a for a in argv if not a.startswith("-")]
    outdir = Path(positional[0]) if positional else (
        Path(os.environ.get("TEMP", "/tmp")) / "pingpair_welcome_reference"
    )
    outdir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv)
    if not _load_inter(app):
        print("WARNING: Inter not found — text will render as boxes.")

    # intro (the welcome card, _index = -1) then QUICK_START_CARDS in order.
    slots = [("intro", -1)] + [(f"c{i + 1}", i) for i in range(len(QUICK_START_CARDS))]

    for mode, name in ((ThemeMode.DARK, "dark"), (ThemeMode.LIGHT, "light")):
        apply_theme(app, mode)
        dlg = WelcomeDialog(dark=(name == "dark"), role="client")
        dlg.resize(_WIN_W, _WIN_H)
        dlg.show()
        for _ in range(10):
            app.processEvents()
        for slot, index in slots:
            # dev tool: intentional private access, mirroring render_app_reference's
            # use of MainWindow._tabs. _index selects the card; _render() repaints it.
            dlg._index = index
            dlg._render()
            for _ in range(8):
                app.processEvents()
            dlg.grab().save(str(outdir / f"welcome_{name}_{slot}.png"), "PNG")
        dlg.close()
        print(f"rendered welcome ({name}) -> {outdir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
