"""First-boot welcome screen + Quick-Start flash-card tour.

Round-22 (EEE); reworked Round-23, then again Round-24. Shown once, right after
the splash, on the very first launch — and the **main window stays hidden**
until the tour is finished or skipped, so nothing flickers behind it. The
dialog is **maximised**.

Round-24 (points 2A-2F):

* The intro/welcome card is **minimal and centred**: just the app logo, the
  two-tone "PingPair" wordmark, a version chip, and the two buttons — **Skip**
  (left) / **Quick Start** (right, no arrow). No body text, no bordered box.
  (2D)
* **Esc no longer dismisses** the dialog — the user must click Skip. (2A)
* **Larger, more readable type** throughout the cards. (2B)
* The tour is **7 cards** (2026-06-02: a Loopback Setup card joined Server /
  Client); Setup is split Server / Client / Loopback, each pinned to that
  role's screenshot via :attr:`pingpair.welcome_cards.Card.role`. (2C)
* Inline figures are **smooth-scaled** to the viewport (crisp, matching the
  "Zoom More" view) rather than relying on the browser's fast scaler. (2E/2F)
* Any card image is **clickable to zoom** (maximised viewer, shared with the
  Help tab).

Card content lives in :mod:`pingpair.welcome_cards`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QFont, QKeyEvent, QPixmap, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, branding, theme
from ..branding import app_icon
from ..paths import HELP_DIR
from ..welcome_cards import HINT_RE, INTRO, QUICK_START_CARDS, Card
from ._image_zoom import (
    ImageZoomDialog,
    NoZoomTextBrowser,
    embed_scaled_image,
    open_zoom,
)

class WelcomeDialog(QDialog):
    """Maximised welcome + Quick-Start tour. ``exec()`` returns when dismissed."""

    def __init__(
        self,
        *,
        dark: bool,
        role: str = "client",
        parent: QWidget | None = None,
        start_in_tour: bool = False,
    ) -> None:
        super().__init__(parent)
        self._dark = dark
        self._role = role if role in ("server", "client", "loopback") else "client"
        # -1 = the intro/welcome card; 0..n-1 = a Quick-Start flash card.
        self._index = 0 if start_in_tour else -1
        self._zoom_dialog: ImageZoomDialog | None = None
        self.setWindowTitle("Welcome to PingPair")
        self.setModal(True)
        self.resize(1100, 760)
        # Open maximised.
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        # ---- minimal intro panel (shown only on the welcome card) --------
        self._intro_panel = self._build_intro_panel()
        outer.addWidget(self._intro_panel, stretch=1)

        # ---- tour panel: header (logo + title + version + step) + body ---
        self._tour_panel = QWidget()
        tour = QVBoxLayout(self._tour_panel)
        tour.setContentsMargins(0, 0, 0, 0)
        tour.setSpacing(14)

        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(44, 44))
        header.addWidget(logo)
        self._heading = QLabel()
        self._heading.setStyleSheet("font-size: 20pt; font-weight: 700;")
        header.addWidget(self._heading)
        # Round-27 (point 3A): the version beside the card title was
        # `palette(mid)` grey — barely legible on the dark header (the same
        # low-contrast trap QQQ fixed for the step counter, but the version
        # label was missed). Paint it in the brand accent so it actually reads.
        ver_colour = self._palette()["accent"]
        self._hdr_version = QLabel(f"v{__version__}")
        self._hdr_version.setStyleSheet(
            f"font-size: 11pt; font-weight: 600; color: {ver_colour};"
        )
        self._hdr_version.setAlignment(Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self._hdr_version)
        header.addStretch(1)
        self._step = QLabel()
        # Round-25 (QQQ, point 5): the step counter was palette(mid) grey and
        # hard to read on the dark header — use the high-contrast text colour.
        step_colour = self._palette()["text"]
        self._step.setStyleSheet(f"font-size: 13pt; font-weight: 600; color: {step_colour};")
        self._step.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._step)
        tour.addLayout(header)

        self._browser = NoZoomTextBrowser()
        self._browser.setOpenExternalLinks(False)
        self._browser.setOpenLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        tour.addWidget(self._browser, stretch=1)
        outer.addWidget(self._tour_panel, stretch=1)

        # ---- buttons (shared by both panels) -----------------------------
        btns = QHBoxLayout()
        self._skip = QPushButton("Skip")
        self._skip.setToolTip("Close the tour and go straight to the app.")
        self._skip.clicked.connect(self.accept)
        btns.addWidget(self._skip)
        btns.addStretch(1)
        self._prev = QPushButton("←  Previous")
        self._prev.clicked.connect(self._go_prev)
        btns.addWidget(self._prev)
        self._next = QPushButton("Quick Start")
        self._next.setDefault(True)
        self._next.setAutoDefault(True)
        self._next.clicked.connect(self._go_next)
        btns.addWidget(self._next)
        outer.addLayout(btns)

        self._render()

    # ------------------------------------------------------------------

    def _theme_name(self) -> str:
        """``"dark"`` / ``"light"`` for the active theme."""
        return "dark" if self._dark else "light"

    def _palette(self) -> dict:
        """The active theme's colour palette."""
        return theme.PALETTES[self._theme_name()]

    def _build_intro_panel(self) -> QWidget:
        """The minimal, centred welcome card: logo + wordmark + version (point 2D)."""
        s = self._palette()
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(18)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addStretch(1)

        logo = QLabel()
        logo.setPixmap(branding._draw_icon(132))
        logo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(logo, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Two-tone wordmark matching the splash / Figma "Wordmark".
        wordmark = QLabel(branding.wordmark_html(s))
        wf = QFont()
        wf.setPixelSize(48)
        wf.setWeight(QFont.Weight.DemiBold)
        wordmark.setFont(wf)
        wordmark.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(wordmark)

        version = QLabel(f"v{__version__}")
        version.setStyleSheet(
            f"color: {s['accent']}; background: {s['surface']};"
            " border-radius: 12px; padding: 4px 14px;"
            " font-weight: bold; font-size: 13px;"
        )
        vrow = QHBoxLayout()
        vrow.addStretch(1)
        vrow.addWidget(version)
        vrow.addStretch(1)
        lay.addLayout(vrow)

        # A 2-3 sentence brief on what the app is + who it's for (point 4).
        # A *fixed* width + word-wrap is what makes it wrap to several lines —
        # a maximum-width-only label collapsed to its first clipped line
        # (Round-25 showed only "Automated 20-case LAN").
        if INTRO.body_html:
            brief = QLabel(INTRO.body_html)
            brief.setTextFormat(Qt.TextFormat.RichText)
            brief.setWordWrap(True)
            brief.setFixedWidth(820)
            brief.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            brief.setStyleSheet(
                f"color: {s['subtext']}; font-size: 14pt; line-height: 150%;"
            )
            brow = QHBoxLayout()
            brow.addStretch(1)
            brow.addWidget(brief)
            brow.addStretch(1)
            lay.addLayout(brow)

        lay.addStretch(2)
        return panel

    def _current_card(self) -> Card:
        if self._index < 0:
            return INTRO
        return QUICK_START_CARDS[self._index]

    def _resolve_image(self, name: str, role: str | None = None) -> Path | None:
        """Find a card image: theme ``_assets`` (diagrams) then a role's
        ``_shots`` (real screenshots). ``role`` overrides the running role for
        a card pinned to a specific side (e.g. the Server Setup card). Returns
        an absolute path or None."""
        theme_name = self._theme_name()
        shot_role = role or self._role
        candidates = (
            HELP_DIR / "_assets" / theme_name / name,
            HELP_DIR / "_shots" / theme_name / shot_role / name,
        )
        for path in candidates:
            if path.is_file():
                return path
        return None

    def _image_target_width(self) -> int:
        """Logical width to scale a card figure to — the live viewport, minus
        a small margin. Falls back to a sensible default before the dialog is
        shown (when the viewport is still tiny)."""
        avail = self._browser.viewport().width() - 24
        return max(640, avail)

    def _render(self) -> None:
        total = len(QUICK_START_CARDS)
        on_intro = self._index < 0
        self._intro_panel.setVisible(on_intro)
        self._tour_panel.setVisible(not on_intro)

        if not on_intro:
            card = self._current_card()
            self._heading.setText(card.title)
            self._browser.document().setDefaultStyleSheet(self._css())
            self._browser.setHtml(self._card_html(card))

        if on_intro:
            # Intro/welcome card: the two-button choice, no arrow on Quick Start.
            self._step.setText("")
            self._prev.setVisible(False)
            self._skip.setText("Skip")
            self._next.setText("Quick Start")
        else:
            self._step.setText(f"{self._index + 1} / {total}")
            self._prev.setVisible(True)
            self._skip.setText("Skip tour")
            is_last = self._index >= total - 1
            self._next.setText("Got it  ✓" if is_last else "Next  →")

    def _build_card_figure(self, card: Card) -> str:
        """The card's ``<div class="figure">`` HTML, or ``""`` when the card has
        no image (or it can't be resolved/embedded).

        The figure's zoom link carries the *original* file path (so the viewer
        loads full resolution); the inline ``<img>`` points at a pre-scaled
        in-document resource so it renders sharply (point 2E/2F)."""
        if not card.image:
            return ""
        path = self._resolve_image(card.image, role=card.role)
        if path is None:  # absent (e.g. card-5 popup) -> text only
            return ""
        real = path.as_posix()
        target = self._image_target_width()
        embedded = embed_scaled_image(
            self._browser.document(),
            str(path),
            max_logical_width=target,
            device_pixel_ratio=self._browser.devicePixelRatioF(),
            key="mem://welcome-card",
        )
        if embedded is None:
            img = f'<img src="{real}" width="{target}">'
        else:
            url, width = embedded
            img = f'<img src="{url}" width="{width}">'
        return (
            f'<div class="figure"><a href="zoom:{real}">{img}</a>'
            '<p class="cap">Click the image to enlarge.</p></div>'
        )

    def _card_html(self, card: Card) -> str:
        """Build a card's body HTML, embedding a crisp smooth-scaled figure.

        The whole fragment is wrapped in ``<body>`` so the default stylesheet's
        ``body`` font-size actually binds (Round-26: a bare ``<p>`` fragment
        renders at Qt's 9 pt default, which is why earlier ``body`` font bumps
        had no visible effect — point 6)."""
        figure = self._build_card_figure(card)
        inner = card.body_html
        if not figure:
            # No image rendered -> drop the card's "click the image" hint so the
            # tour doesn't dangle the instruction with nothing to click (3B).
            inner = HINT_RE.sub("", inner).strip()
        return f"<body>{inner}{figure}</body>"

    def _on_anchor_clicked(self, url: QUrl) -> None:
        text = url.toString()
        if not text.startswith("zoom:"):
            return
        path = text[len("zoom:"):]
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        self._zoom_dialog = open_zoom(
            pixmap, self._current_card().title, dark=self._dark, parent=self
        )

    def _go_next(self) -> None:
        total = len(QUICK_START_CARDS)
        if self._index < 0:
            self._index = 0  # leave intro -> first flash card
        elif self._index < total - 1:
            self._index += 1
        else:
            self.accept()  # finished the last card
            return
        self._render()

    def _go_prev(self) -> None:
        # From the first flash card, step back to the intro/welcome card.
        self._index = self._index - 1 if self._index > 0 else -1
        self._render()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # Qt override (camelCase)
        """Swallow Esc so it can't skip the tour (point 2A); ← / → navigate.

        Default ``QDialog`` behaviour maps Esc to ``reject()`` — which would
        dismiss the whole welcome screen on a stray keypress. The user must
        click **Skip** to leave instead. Left / Right step the cards as a
        convenience alongside the Previous / Next buttons.
        """
        key = event.key()
        if key == Qt.Key.Key_Escape:
            event.accept()
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self._go_next()
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_PageUp) and self._index >= 0:
            self._go_prev()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event: QShowEvent) -> None:  # Qt override (camelCase)
        """Re-render once shown so figures scale to the real maximised width.

        ``__init__`` runs before the dialog is on screen, so the viewport is
        still tiny and a figure would be scaled small. Re-rendering here (after
        the maximised geometry applies) makes the first figure crisp.
        """
        super().showEvent(event)
        if self._index >= 0:
            self._render()

    def _css(self) -> str:
        s = self._palette()
        # The font-size lives on EVERY block element, not just ``body`` — a bare
        # ``<p>`` fragment ignored the ``body`` selector and fell back to Qt's
        # 9 pt default (Round-26 root cause of the "font never gets bigger"
        # reports). With ``_card_html`` also wrapping in ``<body>`` and ``p``
        # carrying its own size, the set size actually renders.
        # Round-28 (point 3): the body was bumped to 15 pt while fighting that
        # 9 pt *rendering* bug; once it rendered honestly, 15 pt read oversized
        # next to the heading + diagrams. Dialled back to 13 pt to match the
        # card vibe — still comfortably readable.
        return f"""
        body {{ font-family: 'Inter','Segoe UI',Arial,sans-serif;
                font-size: 13pt; color: {s['text']}; }}
        p {{ font-size: 13pt; line-height: 158%; margin: 0 0 16px 0; }}
        .lead {{ font-size: 14pt; color: {s['subtext']}; }}
        .hint {{ color: {s['subtext']}; font-style: italic; font-size: 12pt; }}
        .cap {{ color: {s['subtext']}; font-style: italic; font-size: 11pt;
                margin: 6px 0 0 0; }}
        b {{ color: {s['text']}; }}
        tt {{ font-family: Consolas,monospace; background: {s['surface']};
              padding: 1px 4px; }}
        .figure {{ margin: 18px 0 0 0; }}
        .figure a {{ text-decoration: none; }}
        .figure img {{ border: 1px solid {s['border_strong']}; }}
        """
