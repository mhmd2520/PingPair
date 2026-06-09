"""Shared full-screen image viewer for help screenshots AND any drawing.

Round-23 (point 15): extracted from ``help_view`` so the Welcome screen and
every help figure (screenshots *and* the rendered diagrams) can all open the
same maximised, high-quality zoom viewer on click.

Inline figures render at the content-pane width, so fine detail (the
prerequisite-table text, status pills) is hard to read. Clicking a figure
opens it here: **fit-to-window** by default (smoothly scaled to the maximised
dialog), with a one-click **"Zoom More"** toggle to 100% native pixels (Round-23
point 13 — renamed from "Actual size (100%)") in a scroll area so the reader
can inspect any region at full resolution. Esc / Close dismisses.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap, QResizeEvent, QShowEvent, QTextDocument, QWheelEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


class NoZoomTextBrowser(QTextBrowser):
    """A ``QTextBrowser`` that never zooms its text on Ctrl+wheel.

    Round-26 (point 5): ``QTextEdit``/``QTextBrowser`` zoom the font on
    Ctrl+scroll by default, which let the Welcome screen and Help guide drift
    to an arbitrary size. We disable that — a Ctrl+wheel scrolls the view
    normally (never rescales the text), so the designed font size is what the
    user always sees.
    """

    def wheelEvent(self, event: QWheelEvent) -> None:  # Qt override (camelCase)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Scroll instead of zoom: feed the delta straight to the scrollbar.
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)


def embed_scaled_image(
    document: QTextDocument,
    abs_path: str,
    *,
    max_logical_width: int,
    device_pixel_ratio: float = 1.0,
    key: str,
    cache: dict | None = None,
) -> tuple[str, int] | None:
    """Register a smooth-scaled copy of an image on ``document`` for crisp inline rendering.

    Round-24 (LLL, points 2E/2F): ``QTextBrowser`` scales ``<img width=N>`` with
    a fast (aliased) sampler, so inline figures looked noticeably softer than the
    "Zoom More" (native-pixel) view. Instead we pre-scale the source **once** with
    :data:`Qt.SmoothTransformation`, tag it with a device-pixel ratio, and hand it
    to the document as an image resource. The browser then blits it as sharp as
    the source allows with no runtime rescale.

    The figure is laid out at ``logical_width`` = ``min(max_logical_width,
    source_px)`` — it fills the requested width whenever the source is wide
    enough, and is **never upscaled** past its native size (which would blur).
    Crucially the logical width does *not* shrink as the display's device-pixel
    ratio rises (the bug a naive ``round(width/dpr)`` would cause on 125%/150%
    Windows scaling): we pick a device-pixel ratio that keeps the figure at
    ``logical_width`` while spending every available source/target pixel on
    sharpness.

    The returned ``(url, logical_width)`` feed an
    ``<img src="{url}" width="{logical_width}">`` tag; ``None`` means the file
    couldn't be loaded. Pass a ``cache`` dict to memoise the scaled pixmap across
    re-renders (keyed by path + width + dpr) so the source isn't re-decoded and
    re-scaled every time.
    """
    dpr = device_pixel_ratio if device_pixel_ratio and device_pixel_ratio > 0 else 1.0
    cache_key = (abs_path, int(max_logical_width), round(dpr, 3))
    if cache is not None and cache_key in cache:
        pixmap, logical_width = cache[cache_key]
    else:
        pixmap = QPixmap(abs_path)
        if pixmap.isNull():
            return None
        source_px = pixmap.width()
        logical_width = max(1, min(int(max_logical_width), source_px))
        target_px = max(1, round(logical_width * dpr))
        if source_px > target_px:
            # More pixels than we need even at full DPR -> smooth-downscale,
            # then render at the display's DPR.
            pixmap = pixmap.scaledToWidth(
                target_px, Qt.TransformationMode.SmoothTransformation
            )
            out_dpr = dpr
        else:
            # Source can't reach target_px. Use all its pixels and choose a DPR
            # that still fills logical_width (source_px >= logical_width here, so
            # out_dpr >= 1) — the figure stays full-width and crisp on HiDPI.
            out_dpr = source_px / logical_width
        pixmap.setDevicePixelRatio(out_dpr)
        if cache is not None:
            cache[cache_key] = (pixmap, logical_width)
    document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(key), pixmap)
    return key, logical_width


class ImageZoomDialog(QDialog):
    """Maximised viewer for a screenshot or diagram.

    Opens **fit-to-window** (smoothly scaled to the maximised viewport); the
    toggle flips to **"Zoom More"** → 100% native pixels for the sharpest
    read (raster scaling interpolates, native pixels don't — that's why the
    100% view is crispest).
    """

    def __init__(
        self,
        pixmap: QPixmap,
        caption: str,
        *,
        dark: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self._fit = True
        self.setWindowTitle(caption or "Image")
        self.setSizeGripEnabled(True)
        backdrop = "#0b0b0f" if dark else "#1f2937"
        self.setStyleSheet(
            f"QDialog {{ background: {backdrop}; }}"
            " QLabel#cap { color: #cbd5e1; font-size: 10pt; }"
            " QPushButton { padding: 5px 14px; }"
        )

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setWidget(self._label)

        self._toggle = QPushButton()
        self._toggle.clicked.connect(self._toggle_zoom)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        cap = QLabel(f"{caption}  ·  Esc or Close to dismiss")
        cap.setObjectName("cap")
        cap.setWordWrap(True)

        bar = QHBoxLayout()
        bar.addWidget(cap, stretch=1)
        bar.addWidget(self._toggle)
        bar.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self._scroll, stretch=1)
        layout.addLayout(bar)

        self._apply_zoom()

    def _toggle_zoom(self) -> None:
        self._fit = not self._fit
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        """Repaint either fit-to-viewport or at 100% native pixels."""
        if self._fit:
            # "Zoom More" -> switch to native-pixel (sharpest) view.
            self._toggle.setText("Zoom More")
            viewport = self._scroll.viewport().size()
            scaled = self._pixmap.scaled(
                viewport,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._label.setPixmap(scaled)
            self._label.resize(scaled.size())
        else:
            self._toggle.setText("Fit to window")
            self._label.setPixmap(self._pixmap)
            self._label.resize(self._pixmap.size())

    def showEvent(self, event: QShowEvent) -> None:  # Qt override (camelCase)
        """Re-fit once the dialog is actually on screen at its final size.

        ``__init__`` runs before the widget is shown, so the viewport size is
        still tiny then — fitting against it would scale the image to a few
        pixels and look blurry on first paint. Re-applying here (after the
        maximised geometry is in effect) makes the first view sharp."""
        super().showEvent(event)
        if self._fit:
            self._apply_zoom()

    def resizeEvent(self, event: QResizeEvent) -> None:  # Qt override (camelCase)
        """Keep a fit-to-window image filling the viewport as the dialog grows."""
        super().resizeEvent(event)
        if self._fit:
            self._apply_zoom()


def open_zoom(
    pixmap: QPixmap, caption: str, *, dark: bool, parent: QWidget | None = None
) -> ImageZoomDialog:
    """Build + show a maximised :class:`ImageZoomDialog`; return it.

    The caller must keep a reference (Qt won't) until it's dismissed.
    """
    dialog = ImageZoomDialog(pixmap, caption, dark=dark, parent=parent)
    dialog.showMaximized()
    return dialog
