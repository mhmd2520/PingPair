"""Launch loading splash.

A frameless, centred window shown briefly on every launch, then the main
window appears. Shows only the locked paired-nodes logo, the "PingPair"
wordmark and the version — nothing else. Honours the active Light / Dark
theme via the resolved :data:`pingpair.theme.PALETTES` spec.

Orchestrated from :func:`pingpair.app.launch_gui`: the splash is shown
first (so it paints instantly), the heavy :class:`MainWindow` is built
while it's up, and :attr:`finished` (fired ``SPLASH_DURATION_MS`` after
the splash appears) reveals the window. Set ``PINGPAIR_NO_SPLASH=1`` to
skip it entirely (dev / debugger / automated launches).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import branding
from ..theme import PALETTES

SPLASH_DURATION_MS = 2000


class LoadingSplash(QWidget):
    """Frameless centred startup splash; emits :attr:`finished` when done."""

    finished = Signal()

    def __init__(
        self,
        effective_theme: str,
        version: str,
        duration_ms: int = SPLASH_DURATION_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._duration_ms = max(200, int(duration_ms))
        self._started = False

        spec = PALETTES.get(effective_theme, PALETTES["dark"])
        self._build_ui(spec, version)
        self.setFixedSize(400, 300)

    def _build_ui(self, spec: dict, version: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("splashCard")
        card.setStyleSheet(
            "#splashCard {"
            f" background: {spec['window']};"
            f" border: 1px solid {spec['border_strong']};"
            " border-radius: 16px; }"
        )
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(40, 36, 40, 36)
        lay.setSpacing(14)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        logo = QLabel()
        logo.setPixmap(branding._draw_icon(120))
        logo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(logo, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Two-tone wordmark matching the Figma "Wordmark" (white "Ping" +
        # cyan "Pair"); on Light the "Ping" half is the dark text colour.
        title = QLabel(branding.wordmark_html(spec))
        tf = QFont()
        tf.setPixelSize(34)
        tf.setWeight(QFont.Weight.DemiBold)
        title.setFont(tf)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(title)

        ver = QLabel(f"v{version}")
        ver.setStyleSheet(
            f"color: {spec['accent']}; background: {spec['surface']};"
            " border-radius: 11px; padding: 3px 12px;"
            " font-weight: bold; font-size: 12px;"
        )
        ver_row = QHBoxLayout()
        ver_row.addStretch(1)
        ver_row.addWidget(ver)
        ver_row.addStretch(1)
        lay.addLayout(ver_row)

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.center().x() - self.width() // 2,
            geo.center().y() - self.height() // 2,
        )

    def _on_done(self) -> None:
        self.finished.emit()

    def start(self) -> None:
        """Begin the completion timer (idempotent)."""
        if self._started:
            return
        self._started = True
        QTimer.singleShot(self._duration_ms, self._on_done)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._center_on_screen()
        self.start()
