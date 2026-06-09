"""Application icon — the PingPair "paired nodes + ping pulse" mark.

Two cyan endpoints joined by a ping pulse on a slate badge: the
two-laptop pairing the app characterizes. Rendered at runtime from a
single embedded SVG so the window/taskbar icon and the PyInstaller
``.ico`` share one definition and need no external asset to be present.

``ICON_SVG`` is the canonical source. It is kept byte-identical to
``resources/logo.svg`` (the on-disk design asset, used by docs and the
future welcome screen) and to the "PingPair - Brand" Figma file. If the
mark changes, update all three.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="256" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#1E293B"/>
      <stop offset="1" stop-color="#0F172A"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="256" height="256" rx="56" fill="url(#bg)"/>
  <path d="M72 128 L110 128 L122 96 L134 158 L146 128 L184 128" fill="none"
        stroke="#22D3EE" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="72" cy="128" r="20" fill="#06B6D4"/>
  <circle cx="184" cy="128" r="20" fill="#06B6D4"/>
  <circle cx="72" cy="128" r="8" fill="#ECFEFF"/>
  <circle cx="184" cy="128" r="8" fill="#ECFEFF"/>
</svg>"""


def _draw_icon(size: int = 256) -> QPixmap:
    """Render the PingPair mark to a transparent ``size``×``size`` pixmap."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    renderer = QSvgRenderer(QByteArray(ICON_SVG.encode("utf-8")))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p)
    p.end()
    return pix


def logo_png_bytes(size: int = 256) -> bytes:
    """Rasterize the PingPair mark to transparent PNG bytes, ``size`` px square.

    Unlike :func:`app_icon` / :func:`_draw_icon`, this paints onto a
    :class:`QImage` (the raster paint device) rather than a ``QPixmap`` — so it
    works **without a running QApplication**. The report writers call it to
    embed the logo in the Word / PDF report headers, and they run headless
    (worker thread / unit tests) where no GUI app exists. Output is a PNG-encoded
    transparent square rendered straight from the vector :data:`ICON_SVG`, so the
    report logo always tracks the one canonical mark.

    This primitive raises on failure; it is **not** safe-by-default. The
    report-side failure-tolerance (omit the logo rather than break the report)
    lives in :func:`pingpair.reporting._logo._logo_png`, not here.
    """
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))

    renderer = QSvgRenderer(QByteArray(ICON_SVG.encode("utf-8")))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p)
    p.end()

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    # The format arg must be the str "PNG" at runtime; PySide6's stub mistypes it
    # as bytes, so the ignore documents that stub bug rather than papering a real one.
    img.save(buf, "PNG")  # type: ignore[call-overload]
    buf.close()
    return bytes(ba.data())


def wordmark_html(spec: dict) -> str:
    """Two-tone "PingPair" wordmark markup for a rich-text ``QLabel``.

    White (dark theme) / dark (light theme) "Ping" + accent-cyan "Pair",
    matching the Figma "Wordmark". Shared by the launch splash and the welcome
    screen so a brand-colour change lives in one place. ``spec`` is a
    :data:`pingpair.theme.PALETTES` entry; sizing/weight stay with each caller.
    """
    return (
        f'<span style="color:{spec["text"]};">Ping</span>'
        f'<span style="color:{spec["accent"]};">Pair</span>'
    )


def app_icon() -> QIcon:
    """Return a multi-resolution :class:`QIcon` for the app/window icon."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw_icon(size))
    return icon


def write_ico_file(dest: Path, sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)) -> None:
    """Persist the mark as a **multi-resolution** ``.ico`` (PyInstaller build).

    Windows picks the closest embedded size per view (16 px taskbar → 256 px
    "extra large icons"). A single-size ICO makes Windows scale one slot for
    every view, which looks blocky/pixelated at the large views — exactly the
    bug this fixes. So we embed every size in ``sizes``, each **rendered
    directly from the vector mark** (not downsampled from one big raster), then
    write them as one ICO with Pillow, whose ICO encoder produces a proper
    multi-image file (with the 256 px slot PNG-compressed) — unlike Qt's ICO
    writer, which only ever emits a single image.

    Falls back to Qt's writer (single image) if Pillow is unavailable, so a dev
    run is never blocked.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    ordered = tuple(sorted(set(sizes)))
    try:
        from PIL import Image  # bundled (matplotlib dep)
    except ImportError:
        _write_ico_via_qt(dest, max(ordered))
        return

    frames = [_qpixmap_to_pil(_draw_icon(s)) for s in ordered]
    largest = frames[-1]
    # Pillow embeds one entry per (w, h) in ``sizes``; passing the largest frame
    # plus the rest via ``append_images`` keeps each slot's own crisp render.
    largest.save(
        dest,
        format="ICO",
        sizes=[(s, s) for s in ordered],
        append_images=frames[:-1],
    )


def _qpixmap_to_pil(pix: "QPixmap"):
    """Convert a QPixmap to a Pillow RGBA image via an in-memory PNG.

    The PNG round-trip sidesteps QImage stride/format pitfalls of poking at
    raw bits, and the mark is tiny so the cost is negligible.
    """
    import io

    from PIL import Image
    from PySide6.QtCore import QBuffer, QByteArray

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    buf.close()
    return Image.open(io.BytesIO(bytes(ba))).convert("RGBA")


def _write_ico_via_qt(dest: Path, size: int) -> None:
    """Fallback single-image ICO (or PNG) when Pillow isn't importable."""
    from PySide6.QtGui import QImageWriter

    img = _draw_icon(size).toImage()
    writer = QImageWriter(str(dest), b"ico")
    if not writer.canWrite():
        img.save(str(dest), "PNG")
        return
    writer.write(img)
