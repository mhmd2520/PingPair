"""Recolour the white bleed in the corners of the Help topology diagrams.

Round-21 (point 2, 2026-05-26). The Figma export of the rounded topology
frames baked an opaque near-white (#F5F5F5) into the four corner triangles
that sit *outside* the frame's rounded corners. On the dark Help page those
read as glaring white triangles. The card itself fills the rest of the
bounding box (dark #0E1117 / light #EEF2F7), so flood-filling each corner
with that card-fill colour squares the outer frame and removes the artefact —
opaque, no transparency, theme-correct.

Idempotent: re-running on an already-fixed PNG is a no-op (the corners are
already the card colour, so there is no near-white region to flood).

Run from the repo's ``Software`` dir:
    .venv\\Scripts\\python.exe tools\\fix_topology_corners.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

_ASSETS = Path(__file__).resolve().parent.parent / (
    "src/pingpair/resources/help/_assets"
)
_PNGS = ("topology.png", "topology-loopback.png")

# The card fill that should replace the white bleed, per theme. These match
# the Help page surface (theme.py) so the squared frame blends with the page.
_CARD_FILL = {
    "dark": (14, 17, 23, 255),
    "light": (238, 242, 247, 255),
}

# Target the white bleed precisely: it is *neutral* (R=G=B≈245). The light
# card (#EEF2F7 = 238,242,247) is non-neutral (B-R=9) and pure-white text is
# 255, so neither is touched. We avoid PIL.floodfill — its `thresh` bleeds
# into the light card, which is within threshold of the white seed.
_LO, _HI = 240, 250  # corner bleed is 245; excludes 238 card and 255 text


def _is_white_bleed(r: int, g: int, b: int) -> bool:
    return (
        _LO <= r <= _HI
        and _LO <= g <= _HI
        and _LO <= b <= _HI
        and abs(r - g) <= 3
        and abs(g - b) <= 3
    )


def fix_one(path: Path, fill: tuple[int, int, int, int]) -> int:
    im = Image.open(path).convert("RGBA")
    px = im.load()
    w, h = im.size
    n = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if _is_white_bleed(r, g, b):
                px[x, y] = fill
                n += 1
    if n:
        im.save(path)
    return n


def main() -> None:
    for theme in ("dark", "light"):
        fill = _CARD_FILL[theme]
        for name in _PNGS:
            p = _ASSETS / theme / name
            if not p.exists():
                print(f"skip (missing): {p}")
                continue
            n = fix_one(p, fill)
            print(f"{'fixed' if n else 'clean'} {theme}/{name} ({n} px)")


if __name__ == "__main__":
    main()
