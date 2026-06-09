"""Render the Help-guide diagrams as square-bordered, theme-matched PNGs.

Round-22 (CCC + DDD). Replaces the Figma topology exports — whose rounded
frames left near-white corner triangles on the dark Help page (the "white
corners" / "curvy borders" finding) — with diagrams drawn here in
matplotlib. Every shape is a sharp-cornered ``Rectangle`` and the canvas is
filled edge-to-edge with the theme's panel colour, so there are **no rounded
corners and no white bleed** anywhere, on either theme.

It also adds the workflow diagrams the user asked to see in the app:

* ``workflow``          — the tab-relationship map (Prepare → Run → Results).
* ``quickstart``        — the 4-step Quick-Start setup sequence.
* ``case-grid``         — the 20-case payload x bandwidth grid.
* ``report-artifacts``  — what one sweep writes to disk.
* ``control-sequence``  — the Client/Server control-channel message flow.

Output: ``resources/help/_assets/<dark|light>/<name>.png`` — the same
theme-matched folder ``help_view._assets_dir()`` already searches (so the
section HTML just needs ``<img src="<name>.png">``). These live OUTSIDE
``_shots/`` so ``build_help_shots.py`` (which wipes ``_shots``) never deletes
them.

Run:  Software\\.venv\\Scripts\\python.exe Software\\tools\\build_help_diagrams.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, Rectangle

# Palette subset mirrored from pingpair.theme.PALETTES so the diagrams stay
# on-brand without importing Qt. Keep in sync if the theme palette changes.
THEME: dict[str, dict[str, str]] = {
    "dark": {
        "canvas": "#0b1220",
        "panel": "#16213a",
        "panelb": "#223152",
        "edge": "#4d6790",
        "accent": "#22d3ee",
        "text": "#f1f5f9",
        "sub": "#94a3b8",
        "green": "#34d399",
        "blue": "#60a5fa",
        "amber": "#fbbf24",
    },
    "light": {
        "canvas": "#ffffff",
        "panel": "#f1f6fc",
        "panelb": "#e8eef6",
        "edge": "#8aa1c0",
        "accent": "#0891b2",
        "text": "#0f1e36",
        "sub": "#5b6b85",
        "green": "#059669",
        "blue": "#2563eb",
        "amber": "#b45309",
    },
}

# Square coordinate space every draw function works in.
W, H = 160.0, 90.0

ASSETS = (
    Path(__file__).resolve().parent.parent
    / "src" / "pingpair" / "resources" / "help" / "_assets"
)


# --------------------------------------------------------------------------
# Low-level drawing helpers — every box is a sharp-cornered Rectangle.
# --------------------------------------------------------------------------


def _rect(ax, x, y, w, h, *, fc, ec, lw=1.8, z=2):
    ax.add_patch(
        Rectangle((x, y), w, h, facecolor=fc, edgecolor=ec, linewidth=lw, zorder=z)
    )


def _txt(ax, x, y, s, *, color, size=12, weight="normal", ha="left", va="center", z=4):
    ax.text(
        x, y, s, color=color, fontsize=size, fontweight=weight,
        ha=ha, va=va, zorder=z,
    )


def _arrow(ax, x1, y1, x2, y2, *, color, lw=2.2, style="-|>"):
    ax.annotate(
        "", xy=(x2, y2), xytext=(x1, y1),
        arrowprops=dict(arrowstyle=style, color=color, lw=lw, shrinkA=2, shrinkB=2),
        zorder=3,
    )


def _title(ax, P, title, subtitle):
    _txt(ax, 8, H - 9, title, color=P["accent"], size=21, weight="bold")
    if subtitle:
        _txt(ax, 8, H - 17, subtitle, color=P["sub"], size=12)


# --------------------------------------------------------------------------
# Diagrams
# --------------------------------------------------------------------------


def draw_topology(ax, P, *, loopback: bool) -> None:
    if loopback:
        _title(ax, P, "Loopback dev mode",
                "One PC plays both roles on 127.0.0.1 — same 20-case grid, no cable.")
        bx, by, bw, bh = 50, 30, 60, 38
        _rect(ax, bx, by, bw, bh, fc=P["panel"], ec=P["edge"])
        ax.add_patch(Circle((bx + 6, by + bh - 7), 1.6, color=P["accent"], zorder=4))
        _txt(ax, bx + 10, by + bh - 7, "This PC", color=P["text"], size=15, weight="bold")
        _txt(ax, bx + 6, by + bh - 15, "SERVER + CLIENT", color=P["accent"], size=11, weight="bold")
        _txt(ax, bx + 6, by + bh - 24, "127.0.0.1", color=P["text"], size=22, weight="bold")
        _txt(ax, bx + 6, by + 6, "iperf3 -s  +  iperf3 -c  ·  fping  ·  20 cases",
             color=P["sub"], size=11)
        # self-loop arrow
        _arrow(ax, bx + bw, by + bh - 10, bx + bw + 9, by + bh - 10, color=P["accent"])
        _arrow(ax, bx + bw + 9, by + 10, bx + bw, by + 10, color=P["accent"])
        ax.add_patch(Rectangle((bx + bw + 9, by + 10), 0.01, bh - 20,
                     edgecolor=P["accent"], facecolor="none", lw=2.2, zorder=3))
        _txt(ax, bx + bw + 12, by + bh / 2, "loopback", color=P["sub"], size=10, ha="left")
        return

    _title(ax, P, "Standard test topology",
           "Two laptops wired back-to-back over one Ethernet cable — no switch or router.")
    # Laptop A (Server)
    ax_, ay, aw, ah = 8, 28, 56, 40
    _rect(ax, ax_, ay, aw, ah, fc=P["panel"], ec=P["edge"])
    ax.add_patch(Circle((ax_ + 6, ay + ah - 7), 1.6, color=P["green"], zorder=4))
    _txt(ax, ax_ + 10, ay + ah - 7, "Laptop A", color=P["text"], size=15, weight="bold")
    _txt(ax, ax_ + 6, ay + ah - 15, "SERVER role", color=P["green"], size=11, weight="bold")
    _txt(ax, ax_ + 6, ay + ah - 24, "192.168.1.1", color=P["text"], size=21, weight="bold")
    _txt(ax, ax_ + 6, ay + 9, "/24 · listens & obeys", color=P["sub"], size=11)
    _txt(ax, ax_ + 6, ay + 4, "iperf3 -s · control TCP 5202", color=P["sub"], size=10)
    # Laptop B (Client)
    bx = W - 8 - 56
    _rect(ax, bx, ay, aw, ah, fc=P["panel"], ec=P["edge"])
    ax.add_patch(Circle((bx + 6, ay + ah - 7), 1.6, color=P["blue"], zorder=4))
    _txt(ax, bx + 10, ay + ah - 7, "Laptop B", color=P["text"], size=15, weight="bold")
    _txt(ax, bx + 6, ay + ah - 15, "CLIENT role", color=P["blue"], size=11, weight="bold")
    _txt(ax, bx + 6, ay + ah - 24, "192.168.1.2", color=P["text"], size=21, weight="bold")
    _txt(ax, bx + 6, ay + 9, "/24 · drives the schedule", color=P["sub"], size=11)
    _txt(ax, bx + 6, ay + 4, "iperf3 -c · fping · 20 cases", color=P["sub"], size=10)
    # Ethernet link
    midy = ay + ah / 2
    ax.add_line(Line2D([ax_ + aw, bx], [midy, midy], color=P["accent"], lw=4, zorder=3))
    _txt(ax, W / 2, midy + 5, "Ethernet", color=P["accent"], size=13, weight="bold", ha="center")
    _txt(ax, W / 2, midy - 5, "direct cable", color=P["sub"], size=10, ha="center")
    # Legend pills (square)
    pills = [("Control · TCP 5202", P["accent"]), ("iperf3 · 5201", P["blue"]),
             ("fping · ICMP", P["green"])]
    px = 8
    for label, dot in pills:
        pw = 3.2 + len(label) * 1.18
        _rect(ax, px, 12, pw, 7, fc=P["panelb"], ec=P["edge"], lw=1.2)
        ax.add_patch(Circle((px + 3, 15.5), 1.1, color=dot, zorder=4))
        _txt(ax, px + 6, 15.5, label, color=P["text"], size=10)
        px += pw + 4


def _flow_box(ax, P, x, y, w, h, title, sub, *, accent=None):
    """One labelled box: accent bar, bold title, single grey sub-line."""
    accent = accent or P["accent"]
    _rect(ax, x, y, w, h, fc=P["panel"], ec=P["edge"])
    ax.add_line(Line2D([x, x], [y, y + h], color=accent, lw=4, zorder=4))
    _txt(ax, x + 5, y + h - 5.5, title, color=P["text"], size=13, weight="bold")
    _txt(ax, x + 5, y + 5, sub, color=P["sub"], size=10)


def draw_workflow(ax, P) -> None:
    _title(ax, P, "How the tabs work together",
           "Prepare the link, run the sweep, then read the results. Help backs every step.")
    bh = 15
    # Prepare column (3 stacked)
    col_x, col_w = 6, 42
    _txt(ax, col_x, 62, "PREPARE", color=P["accent"], size=11, weight="bold")
    prep = [
        ("Setup", "Role + IP · prereqs · Fix all"),
        ("Ping", "Smoke-test the link"),
        ("Config", "20-case grid + IPs"),
    ]
    ys = [43, 25, 7]
    for (t, s), y in zip(prep, ys, strict=True):
        _flow_box(ax, P, col_x, y, col_w, bh, t, s)
    # Run (center)
    run_x, run_w, run_y = 64, 32, 25
    _flow_box(ax, P, run_x, run_y, run_w, bh, "Run",
              "Drive / obey the sweep", accent=P["green"])
    run_cy = run_y + bh / 2
    # Results column
    res_x, res_w = 114, 40
    _txt(ax, res_x, 62, "RESULTS", color=P["accent"], size=11, weight="bold")
    res = [
        ("Save Options", "Formats · folder · metadata"),
        ("Analysis", "Overlay & compare sweeps"),
    ]
    res_ys = [42, 16]
    for (t, s), y in zip(res, res_ys, strict=True):
        _flow_box(ax, P, res_x, y, res_w, bh, t, s, accent=P["blue"])
    # Arrows: each prepare box -> Run; Run -> each results box
    for y in ys:
        _arrow(ax, col_x + col_w, y + bh / 2, run_x, run_cy, color=P["sub"], lw=1.6)
    for y in res_ys:
        _arrow(ax, run_x + run_w, run_cy, res_x, y + bh / 2, color=P["sub"], lw=1.6)
    # Help/About reference strip
    _rect(ax, 6, 1, 148, 6, fc=P["panelb"], ec=P["edge"], lw=1.2)
    _txt(ax, 10, 4, "Help — step-by-step guidance for every tab, Troubleshooting, "
         "and the fping / iperf3 references.  ·  About — version & credits.",
         color=P["sub"], size=10)


def draw_quickstart(ax, P) -> None:
    _title(ax, P, "Quick Start — five-minute path",
           "Or pick Loopback in Setup to try the whole flow on one PC, no cable.")
    steps = [
        ("1", "Wire & launch", ["Ethernet between the", "two laptops; launch", "PingPair on both."]),
        ("2", "Setup: go green", ["Open Setup, click", "Fix all, confirm the", "role banner."]),
        ("3", "Run the sweep", ["On the Client press", "Run full sweep —", "~16 minutes."]),
        ("4", "Collect report", ["Save Options writes", "docx + xlsx + json", "per sweep."]),
    ]
    x = 6
    bw, bh = 33, 34
    cy = 30
    for i, (num, title, lines) in enumerate(steps):
        _rect(ax, x, cy, bw, bh, fc=P["panel"], ec=P["edge"])
        ax.add_patch(Circle((x + 6, cy + bh - 6), 3.2, color=P["accent"], zorder=4))
        _txt(ax, x + 6, cy + bh - 6, num, color=P["canvas"], size=13, weight="bold", ha="center")
        _txt(ax, x + 12, cy + bh - 6, title, color=P["text"], size=12.5, weight="bold")
        yy = cy + bh - 15
        for line in lines:
            _txt(ax, x + 5, yy, line, color=P["sub"], size=10)
            yy -= 5.5
        if i < len(steps) - 1:
            _arrow(ax, x + bw, cy + bh / 2, x + bw + 5.5, cy + bh / 2, color=P["accent"])
        x += bw + 5.5
    _rect(ax, 6, 12, 148, 9, fc=P["panelb"], ec=P["edge"], lw=1.2)
    _txt(ax, 10, 16.5, "This tour lives in Help → Overview any time. Every tab has its own "
         "Help section for the details.", color=P["sub"], size=10.5)


def draw_case_grid(ax, P) -> None:
    _title(ax, P, "The 20-case test grid",
           "Four payloads x five bandwidths = 20 cases, run in sequence (~30 s each).")
    payloads = [200, 600, 1000, 1300]
    bws = [10, 30, 50, 70, 90]
    gx, gy = 26, 16
    cw, ch = 23, 11
    # column headers (bandwidth)
    _txt(ax, gx - 3, gy + 4 * ch + 6, "Payload (B)  ╲  Bandwidth (Mbps)",
         color=P["sub"], size=10, ha="left")
    for j, bw in enumerate(bws):
        _txt(ax, gx + j * cw + cw / 2, gy + 4 * ch + 1, f"{bw}",
             color=P["accent"], size=12, weight="bold", ha="center")
    n = 1
    for i, pay in enumerate(payloads):
        row_y = gy + (3 - i) * ch
        _txt(ax, gx - 4, row_y + ch / 2, f"{pay}", color=P["accent"], size=12,
             weight="bold", ha="right")
        for j in range(len(bws)):
            _rect(ax, gx + j * cw, row_y, cw - 1.5, ch - 1.5, fc=P["panel"], ec=P["edge"], lw=1.2)
            _txt(ax, gx + j * cw + (cw - 1.5) / 2, row_y + (ch - 1.5) / 2,
                 f"{n:02d}", color=P["text"], size=12, weight="bold", ha="center")
            n += 1


def draw_report_artifacts(ax, P) -> None:
    _title(ax, P, "What one sweep saves",
           "Each run gets its own folder under Reports — open it on the Save Options tab.")
    # folder box
    fx, fy, fw, fh = 8, 40, 48, 16
    _rect(ax, fx, fy, fw, fh, fc=P["panel"], ec=P["accent"], lw=2.4)
    _txt(ax, fx + 4, fy + fh - 5, "Reports/", color=P["sub"], size=11)
    _txt(ax, fx + 4, fy + 6, "PingPair_<timestamp>/", color=P["text"], size=13, weight="bold")
    files = [
        ("report.docx", "Word report (Table-1)", P["accent"], True),
        ("report.xlsx", "spreadsheet + raw data", P["accent"], True),
        ("sidecar.json", "the config that ran it", P["green"], True),
        ("report.pdf", "optional", P["blue"], False),
        ("report.txt", "optional", P["blue"], False),
    ]
    rx = 84
    ry = 70
    for name, desc, dot, always in files:
        ry -= 13
        _rect(ax, rx, ry, 66, 10, fc=P["panelb"], ec=P["edge"], lw=1.4)
        ax.add_patch(Circle((rx + 4, ry + 5), 1.4, color=dot, zorder=4))
        _txt(ax, rx + 8, ry + 6.5, name, color=P["text"], size=11.5, weight="bold")
        _txt(ax, rx + 8, ry + 2.5, desc, color=P["sub"], size=9.5)
        tag = "always" if always else "if ticked"
        _txt(ax, rx + 64, ry + 5, tag, color=(P["green"] if always else P["sub"]),
             size=9, ha="right")
        _arrow(ax, fx + fw, fy + fh / 2, rx, ry + 5, color=P["sub"], lw=1.3)


def draw_control_sequence(ax, P) -> None:
    _title(ax, P, "Control-channel handshake",
           "The Client drives; the Server obeys. Separate from iperf3's own 5201 data port.")
    cx, sx = 36, 120
    top, bot = 58, 9
    for x, label, dot in ((cx, "CLIENT", P["blue"]), (sx, "SERVER", P["green"])):
        _rect(ax, x - 16, top, 32, 7, fc=P["panel"], ec=P["edge"])
        ax.add_patch(Circle((x - 12, top + 3.5), 1.3, color=dot, zorder=5))
        _txt(ax, x + 2, top + 3.5, label, color=P["text"], size=12, weight="bold", ha="center")
        ax.add_line(Line2D([x, x], [bot, top], color=P["edge"], lw=1.6, zorder=2, linestyle=(0, (4, 3))))

    def msg(y, frm, to, text, color):
        _arrow(ax, frm, y, to, y, color=color, lw=2.0)
        _txt(ax, (frm + to) / 2, y + 2.2, text, color=P["text"], size=10,
             ha="center", weight="bold")

    msg(54, cx, sx, "HELLO  (control TCP 5202)", P["accent"])
    msg(49, sx, cx, "READY", P["sub"])
    # loop box
    _rect(ax, cx - 22, 14, sx - cx + 44, 32, fc="none", ec=P["amber"], lw=1.6)
    _txt(ax, cx - 20, 43, "for each of the 20 cases:", color=P["amber"], size=10, weight="bold")
    msg(38, cx, sx, "START_CASE n", P["accent"])
    msg(33, sx, cx, "SERVER_READY", P["sub"])
    _txt(ax, (cx + sx) / 2, 28, "iperf3 + fping run (~30 s)", color=P["sub"], size=9.5, ha="center")
    msg(23, cx, sx, "CASE_DONE", P["accent"])
    msg(18, sx, cx, "SERVER_RESULT", P["sub"])
    msg(11, cx, sx, "FINISH  → both ends save the report", P["green"])


DIAGRAMS = {
    "topology": lambda ax, P: draw_topology(ax, P, loopback=False),
    "topology-loopback": lambda ax, P: draw_topology(ax, P, loopback=True),
    "workflow": draw_workflow,
    "quickstart": draw_quickstart,
    "case-grid": draw_case_grid,
    "report-artifacts": draw_report_artifacts,
    "control-sequence": draw_control_sequence,
}


def render_all() -> list[Path]:
    written: list[Path] = []
    for theme, P in THEME.items():
        out_dir = ASSETS / theme
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, draw in DIAGRAMS.items():
            fig = plt.figure(figsize=(13.0, 7.3), dpi=200)
            fig.patch.set_facecolor(P["canvas"])
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_xlim(0, W)
            ax.set_ylim(0, H)
            ax.set_facecolor(P["canvas"])
            ax.axis("off")
            draw(ax, P)
            path = out_dir / f"{name}.png"
            fig.savefig(str(path), facecolor=P["canvas"], dpi=200)
            plt.close(fig)
            written.append(path)
    return written


if __name__ == "__main__":
    for p in render_all():
        print("wrote", p)
