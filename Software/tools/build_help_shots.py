"""Curate raw IMG screenshots into the shipped, theme+role Help _shots tree.

Mohamed drops raw full-resolution captures into ``D:\\PingPair-PrivDev\\IMG``
(host-only, gitignored). This tool copies the chosen ones — **at full
resolution, no recompression** — into

    resources/help/_shots/<theme>/<role>/<section-key>/<filename>.png

so the Help tab can show the screenshot dedicated to the *running role*
(Server / Client / Loopback) and the *active theme* (Light / Dark).

Two modes::

    python tools/build_help_shots.py            # copy per CATALOG -> _shots
    python tools/build_help_shots.py --sheets    # build banner contact sheets

``--sheets`` writes ``IMG/_contact/ban_*.png`` — each row is the role-banner
strip of one capture. NOTE: the banner strip alone CANNOT tell Light from Dark
(the role banner is coloured in both themes) and cannot see a centred dialog
(the finish popup sits mid-frame) — catalogue a fresh batch from full-frame
views or a content-brightness probe, NOT the strips alone. (Two mis-reads on
the 2026-06-08 batch came from trusting the strips; see
[[verify-and-guard-recurring-fixes]].)

CATALOG is keyed by ``(theme, role)`` -> ``{section_key: {target: HHMMSS}}``,
where ``HHMMSS`` is the time portion of a capture taken on ``CAPTURE_DATE``.
It doubles as the durable record of the **2026-06-08 batch** mapping (the prior
2026-06-02 batch was retired when the UI was re-shot for the v0.1.0
disconnect-detection release; those raw files no longer exist on disk).

Notes on the 2026-06-08 batch:
  * Setup is captured in BOTH states per role/theme: an **all-green** all-pass
    shot (the ``Annotation ...`` files — the wired NIC IP matches the role) for
    ``setup/01-checks-overview``, matching the welcome tour's "go green" cards;
    and an **orange** "IP doesn't match role" shot for ``setup/02-role-mismatch``
    AND ``troubleshooting/01-orange-banner``. Loopback can't role-mismatch, so
    its 02-role-mismatch / orange-banner reuse the Client orange capture (the
    Help text labels it as an illustrative example).
  * ``save-options/02-finish-popup`` is the Client "Sweep complete - save
    report?" popup (130018 dark / 130157 light); only the Client side is
    captured because that welcome card is pinned to Client.

``_src`` resolves a timestamp by globbing ``*<CAPTURE_DATE> <ts>.png`` across
``SRC_ROOTS`` (first match wins), so a ``Screenshot ...`` or ``Annotation ...``
capture resolves identically.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

IMG = Path(r"D:\PingPair-PrivDev\IMG")
# The 2026-06-08 batch is a single flat drop (no EXT/EXT2 follow-ups).
SRC_ROOTS = (IMG,)
CAPTURE_DATE = "2026-06-08"
SHOTS = Path(__file__).resolve().parents[1] / (
    "src/pingpair/resources/help/_shots"
)

# (theme, role) -> {section_key: {target_filename: HHMMSS}}.  2026-06-08 batch.
#  * 01-checks-overview = the all-green "Annotation" Setup shot (NIC IP matches
#    role); 02-role-mismatch + troubleshooting/01-orange-banner = the orange
#    "IP doesn't match role" Setup shot. Loopback can't mismatch, so its
#    01-checks-overview is its own amber "Loopback dev mode" Setup and its
#    02-role-mismatch / orange-banner reuse the Client orange capture.
#  * save-options/02-finish-popup = the Client "Sweep complete - save report?"
#    popup (130018 dark / 130157 light); Client-only (the card is Client-pinned).
CATALOG: dict[tuple[str, str], dict[str, dict[str, str]]] = {
    ("dark", "client"): {
        "setup": {"01-checks-overview": "124804", "02-role-mismatch": "122437"},
        "ping": {"01-overview": "122551"},
        "config": {"01-overview": "122553"},
        "run": {"01-overview": "122555"},
        "save-options": {"01-overview": "122557", "02-finish-popup": "130018"},
        "analysis": {"01-overview": "122600"},
        "troubleshooting": {"01-orange-banner": "122437"},
    },
    ("dark", "server"): {
        "setup": {"01-checks-overview": "124750", "02-role-mismatch": "122617"},
        "ping": {"01-overview": "122619"},
        "config": {"01-overview": "122621"},
        "run": {"01-overview": "122623"},
        "save-options": {"01-overview": "122625"},
        "analysis": {"01-overview": "122627"},
        "troubleshooting": {"01-orange-banner": "122617"},
    },
    ("dark", "loopback"): {
        "setup": {"01-checks-overview": "122308", "02-role-mismatch": "122437"},
        "ping": {"01-overview": "122413"},
        "config": {"01-overview": "122415"},
        "run": {"01-overview": "122418"},
        "save-options": {"01-overview": "122420"},
        "analysis": {"01-overview": "122423"},
        "troubleshooting": {"01-orange-banner": "122437"},
    },
    ("light", "client"): {
        "setup": {"01-checks-overview": "124731", "02-role-mismatch": "122709"},
        "ping": {"01-overview": "122722"},
        "config": {"01-overview": "122724"},
        "run": {"01-overview": "122726"},
        "save-options": {"01-overview": "122729", "02-finish-popup": "130157"},
        "analysis": {"01-overview": "122731"},
        "troubleshooting": {"01-orange-banner": "122709"},
    },
    ("light", "server"): {
        "setup": {"01-checks-overview": "124655", "02-role-mismatch": "122637"},
        "ping": {"01-overview": "122640"},
        "config": {"01-overview": "122642"},
        "run": {"01-overview": "122644"},
        "save-options": {"01-overview": "122646"},
        "analysis": {"01-overview": "122648"},
        "troubleshooting": {"01-orange-banner": "122637"},
    },
    ("light", "loopback"): {
        "setup": {"01-checks-overview": "122740", "02-role-mismatch": "122709"},
        "ping": {"01-overview": "122742"},
        "config": {"01-overview": "122744"},
        "run": {"01-overview": "122746"},
        "save-options": {"01-overview": "122748"},
        "analysis": {"01-overview": "122750"},
        "troubleshooting": {"01-orange-banner": "122709"},
    },
}


def _src(ts: str) -> Path:
    """Resolve a capture by HHMMSS across the source roots (first match wins).

    Globs ``*<CAPTURE_DATE> <ts>.png`` so a ``Screenshot ...`` or an
    ``Annotation ...`` capture resolves the same way (HHMMSS is unique within a
    batch). Returns a non-existent primary-root path if none match, so
    ``build_shots`` reports it as MISSING.
    """
    suffix = f"{CAPTURE_DATE} {ts}.png"
    for root in SRC_ROOTS:
        for candidate in sorted(root.glob(f"*{suffix}")):
            return candidate
    return SRC_ROOTS[0] / f"Screenshot {suffix}"


def build_shots() -> int:
    """Rebuild _shots from CATALOG (wipes the tree first). Returns exit code."""
    if SHOTS.exists():
        shutil.rmtree(SHOTS)
    copied = missing = 0
    for (theme, role), sections in CATALOG.items():
        for key, files in sections.items():
            dest_dir = SHOTS / theme / role / key
            dest_dir.mkdir(parents=True, exist_ok=True)
            for fname, ts in files.items():
                s = _src(ts)
                if not s.is_file():
                    print(f"MISSING {s.name} -> {theme}/{role}/{key}/{fname}")
                    missing += 1
                    continue
                shutil.copy2(s, dest_dir / f"{fname}.png")
                copied += 1
    print(f"copied {copied}, missing {missing} -> {SHOTS}")
    return 1 if missing else 0


def build_sheets() -> int:
    """Write banner-strip contact sheets of every capture for cataloging."""
    from PIL import Image, ImageDraw, ImageFont

    out = IMG / "_contact"
    out.mkdir(parents=True, exist_ok=True)
    y0_frac, y1_frac, target_w, gutter, per_sheet = 0.03, 0.20, 1040, 120, 10
    try:
        font = ImageFont.truetype("arialbd.ttf", 30)
    except OSError:
        font = ImageFont.load_default()

    shots: list[Path] = []
    for root in SRC_ROOTS:
        shots.extend(sorted(root.glob("Screenshot *.png")))
    for s in range((len(shots) + per_sheet - 1) // per_sheet):
        bands = []
        for p in shots[s * per_sheet:(s + 1) * per_sheet]:
            try:
                im = Image.open(p).convert("RGB")
            except Exception as exc:  # pragma: no cover - bad PNG
                print(f"skip {p.name}: {exc}")
                continue
            band = im.crop((0, int(im.height * y0_frac), im.width, int(im.height * y1_frac)))
            band = band.resize((target_w, int(band.height * target_w / band.width)))
            bands.append((p.stem.split(" ")[-1], band))
        if not bands:
            continue
        row_h = max(b.height for _, b in bands) + 10
        sheet = Image.new("RGB", (gutter + target_w + 10, row_h * len(bands) + 8), "#101418")
        draw = ImageDraw.Draw(sheet)
        y = 6
        for label, band in bands:
            draw.text((6, y + 8), label, fill="#7dd3fc", font=font)
            sheet.paste(band, (gutter, y))
            y += row_h
        sheet.save(out / f"ban_{s + 1}.png", "PNG")
    print(f"wrote banner sheets for {len(shots)} captures -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return build_sheets() if "--sheets" in argv else build_shots()


if __name__ == "__main__":
    raise SystemExit(main())
