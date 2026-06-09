"""Light / Dark / System theming for the PingPair GUI.

Built on Qt's **Fusion** style so a custom :class:`QPalette` is honoured
identically on every platform, plus a comprehensive QSS layer that gives
**every** interactive element a visible border and a vibrant cyan accent
(buttons, tabs, inputs, tables, group boxes). Light and dark share the
logo's **cyan-on-slate** identity; only the surface/text roles flip.

``"system"`` follows ``QApplication.styleHints().colorScheme()`` and, via
:func:`connect_system_theme`, live-updates when the OS toggles. Switching
is live — re-applying the palette + stylesheet restyles every open
widget. Custom-painted pyqtgraph charts keep their own colours.

Semantic colours elsewhere in the app (the orange role-warning banner,
green/red prereq-status cells, the blue/green role banner) are left
hardcoded on purpose: they're white-on-saturated and read on both themes.
"""

from __future__ import annotations

from enum import Enum
from string import Template

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette


class ThemeMode(str, Enum):
    """User-selectable appearance mode (stored verbatim in QSettings)."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @classmethod
    def coerce(cls, value: object, default: "ThemeMode" = None) -> "ThemeMode":
        """Best-effort parse; unknown values fall back to ``default``/SYSTEM."""
        fallback = default or cls.SYSTEM
        try:
            return cls(str(value))
        except ValueError:
            return fallback


# Role -> hex. Vibrant: deep navy-slate surfaces under a bright cyan accent
# (dark) / crisp cool-white under a saturated cyan accent (light). The
# *_strong border and accent keys are what give every widget a visible edge.
_DARK = {
    # QPalette roles
    "window": "#0b1220",          # deep navy-slate chrome
    "window_text": "#f1f5f9",
    "base": "#1b2942",            # inputs / tables (lighter than window -> pop)
    "alt_base": "#223152",
    "text": "#f1f5f9",
    "button": "#1f3a5f",          # blue-tinted (not flat grey)
    "button_text": "#f1f5f9",
    "bright_text": "#ffffff",
    "highlight": "#0891b2",       # selection
    "highlight_text": "#ffffff",
    "tooltip_base": "#16213a",
    "tooltip_text": "#f1f5f9",
    "placeholder": "#7c8aa3",
    "link": "#38bdf8",
    "disabled_text": "#5b6b85",
    # QSS extras
    "surface": "#16213a",         # group-box / panel fill
    "subtext": "#94a3b8",
    "border": "#334766",
    "border_strong": "#4d6790",   # clearly visible edge
    "button_hover": "#284b78",
    "accent": "#22d3ee",          # bright cyan
    "accent_hover": "#67e8f9",
    "accent_press": "#0891b2",
    "header_bg": "#16213a",
}

_LIGHT = {
    "window": "#e8eef6",          # cool light blue-grey
    "window_text": "#0f1e36",
    "base": "#ffffff",
    "alt_base": "#f1f6fc",
    "text": "#0f1e36",
    "button": "#ffffff",
    "button_text": "#0f1e36",
    "bright_text": "#ffffff",
    "highlight": "#0891b2",
    "highlight_text": "#ffffff",
    "tooltip_base": "#0f1e36",
    "tooltip_text": "#ffffff",
    "placeholder": "#94a3b8",
    "link": "#0e7490",
    "disabled_text": "#94a3b8",
    "surface": "#ffffff",
    "subtext": "#5b6b85",
    "border": "#b6c6db",
    "border_strong": "#8aa1c0",
    "button_hover": "#e6f6fb",     # cyan tint
    "accent": "#0891b2",
    "accent_hover": "#06b6d4",
    "accent_press": "#0e7490",
    "header_bg": "#dbe5f1",
}

PALETTES: dict[str, dict[str, str]] = {"dark": _DARK, "light": _LIGHT}


def resolve_effective(mode: ThemeMode | str, system_is_dark: bool) -> str:
    """Resolve a mode to a concrete ``"light"`` / ``"dark"`` palette key.

    Pure (no Qt app needed) so it's unit-testable. ``"system"`` — and
    any unrecognised value — resolves via ``system_is_dark`` (follows the
    OS); explicit ``"light"`` / ``"dark"`` ignore it.
    """
    m = mode.value if isinstance(mode, ThemeMode) else str(mode)
    if m == "dark":
        return "dark"
    if m == "light":
        return "light"
    return "dark" if system_is_dark else "light"  # "system" / unknown


def _build_qpalette(spec: dict[str, str]) -> QPalette:
    pal = QPalette()
    c = lambda key: QColor(spec[key])  # noqa: E731 - terse local
    pal.setColor(QPalette.ColorRole.Window, c("window"))
    pal.setColor(QPalette.ColorRole.WindowText, c("window_text"))
    pal.setColor(QPalette.ColorRole.Base, c("base"))
    pal.setColor(QPalette.ColorRole.AlternateBase, c("alt_base"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, c("tooltip_base"))
    pal.setColor(QPalette.ColorRole.ToolTipText, c("tooltip_text"))
    pal.setColor(QPalette.ColorRole.Text, c("text"))
    pal.setColor(QPalette.ColorRole.Button, c("button"))
    pal.setColor(QPalette.ColorRole.ButtonText, c("button_text"))
    pal.setColor(QPalette.ColorRole.BrightText, c("bright_text"))
    pal.setColor(QPalette.ColorRole.Highlight, c("highlight"))
    pal.setColor(QPalette.ColorRole.HighlightedText, c("highlight_text"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, c("placeholder"))
    pal.setColor(QPalette.ColorRole.Link, c("link"))

    disabled = c("disabled_text")
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return pal


# QSS uses ``$name`` placeholders (string.Template) so the many literal
# CSS ``{ }`` braces need no escaping. Every interactive widget gets a
# 1px border in $border_strong and an $accent hover/focus/selected state.
_QSS = Template(
    """
QPushButton {
    background: $button;
    color: $button_text;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 22px;
}
QPushButton:hover { background: $button_hover; border-color: $accent; color: $accent; }
QPushButton:pressed { background: $accent_press; color: $highlight_text; border-color: $accent_press; }
QPushButton:disabled { color: $disabled_text; border-color: $border; }
/* Secondary / in-table buttons that should hug their label (Setup Fix
   actions, Analysis side buttons) — tighter padding so the word fits
   without ballooning the row / stealing space from neighbours. */
QPushButton[compact="true"] { padding: 3px 10px; min-height: 18px; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QAbstractSpinBox {
    background: $base;
    color: $text;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 20px;
    selection-background-color: $accent;
    selection-color: $highlight_text;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus, QAbstractSpinBox:focus {
    border: 2px solid $accent;
}
/* Disabled inputs read as clearly greyed-out (e.g. the Save Options tab's
   Destination / Filename fields while Auto save is off) — muted fill,
   grey text, soft border. Fusion alone leaves them nearly identical. */
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QPlainTextEdit:disabled, QTextEdit:disabled, QAbstractSpinBox:disabled {
    background: $window;
    color: $disabled_text;
    border-color: $border;
}
/* The drop-down arrow + spin-box arrows are drawn from real SVG chevron
   images (see _ARROW_QSS / _write_arrow_assets) appended after this base
   block. The CSS-border "triangle" trick Qt does NOT honour — it renders
   a small filled square instead — which is exactly the box artefact the
   VM screenshots showed. The popup list itself is styled here. */
QComboBox QAbstractItemView { background: $base; color: $text;
    border: 1px solid $border_strong; selection-background-color: $accent;
    selection-color: $highlight_text; }

/* Check boxes & radios — visible indicator border in every theme, accent
   fill when checked (Fusion's native indicator is low-contrast on dark). */
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QCheckBox::indicator { border: 1px solid $border_strong; border-radius: 3px; background: $base; }
QRadioButton::indicator { border: 1px solid $border_strong; border-radius: 9px; background: $base; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }
QRadioButton::indicator:checked { background: $accent; border-color: $accent; }

/* Flat, underline-style tabs (matches the Figma): no box, just text with
   a cyan underline on the selected tab and a thin separator under the row. */
QTabWidget::pane { border: none; border-top: 1px solid $border; top: 0; }
QTabBar { background: transparent; qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: $subtext;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 9px 18px;
    margin: 0 6px 0 0;
}
QTabBar::tab:selected { color: $accent; border-bottom: 2px solid $accent; }
QTabBar::tab:hover:!selected { color: $text; }

QGroupBox {
    background: $surface;
    border: 1px solid $border_strong;
    border-radius: 10px;
    margin-top: 14px;
    padding: 16px 16px 14px 16px;
}
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px;
    color: $accent; font-weight: bold; }

QTableView, QTableWidget {
    background: $base;
    alternate-background-color: $alt_base;
    border: 1px solid $border_strong;
    border-radius: 6px;
    gridline-color: $border_strong;
}
QTableView::item:selected, QTableWidget::item:selected {
    background: $accent; color: $highlight_text;
}
QHeaderView::section {
    background: $header_bg;
    color: $text;
    border: none;
    border-right: 1px solid $border;
    border-bottom: 2px solid $accent;
    padding: 6px 8px;
    font-weight: bold;
}
QTableCornerButton::section { background: $header_bg; border: none; }

QScrollBar:vertical { background: $surface; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: $border_strong; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar:horizontal { background: $surface; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: $border_strong; border-radius: 5px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: $accent; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }

QToolTip { background: $tooltip_base; color: $tooltip_text;
    border: 1px solid $accent; padding: 4px 6px; }
QStatusBar { border-top: 1px solid $border_strong; }
QStatusBar::item { border: none; }
"""
)


# Image-based combo/spin arrows. Appended to the base QSS only when the
# chevron SVGs were written successfully (see _build_qss). When asset
# writing fails (read-only FS, etc.) this block is omitted and Fusion
# draws its own native arrows — never the broken CSS-triangle square.
_ARROW_QSS = Template(
    """
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border-left: 1px solid $border_strong; }
QComboBox::down-arrow { image: url($arrow_down); width: 12px; height: 12px; }

QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: border; width: 18px; background: $button;
    border-left: 1px solid $border_strong; }
QAbstractSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 6px; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 6px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: $accent; }
QAbstractSpinBox::up-arrow { image: url($arrow_up); width: 11px; height: 11px; }
QAbstractSpinBox::down-arrow { image: url($arrow_down); width: 11px; height: 11px; }
"""
)


def _arrow_svg(color: str, *, up: bool) -> bytes:
    """A small chevron SVG (down ``v`` or up ``^``) stroked in ``color``."""
    d = "M4 10 L8 6 L12 10" if up else "M4 6 L8 10 L12 6"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
        f'viewBox="0 0 16 16"><path d="{d}" fill="none" stroke="{color}" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ).encode("utf-8")


def _write_arrow_assets(text_color: str) -> tuple[str, str] | None:
    """Write the down/up chevron SVGs for ``text_color`` to the cache dir.

    Returns ``(down_url, up_url)`` as forward-slash paths suitable for a
    QSS ``url(...)``, or ``None`` if writing failed (caller then falls
    back to Fusion's native arrows). Keyed by colour so a live theme
    switch writes fresh files without clobbering the other theme's.
    """
    try:
        from .paths import user_data_dir

        cache = user_data_dir() / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        key = text_color.lstrip("#")
        down = cache / f"arrow-down-{key}.svg"
        up = cache / f"arrow-up-{key}.svg"
        down.write_bytes(_arrow_svg(text_color, up=False))
        up.write_bytes(_arrow_svg(text_color, up=True))
        return (down.as_posix(), up.as_posix())
    except OSError:
        return None


def _build_qss(spec: dict[str, str]) -> str:
    """Comprehensive border/accent stylesheet for the resolved palette.

    The combo/spin arrows are appended from real SVG chevron images when
    they can be written; otherwise the base stylesheet alone is returned
    and Qt's native Fusion arrows show through.
    """
    base = _QSS.substitute(spec)
    arrows = _write_arrow_assets(spec["text"])
    if arrows is not None:
        down, up = arrows
        with_arrows = dict(spec)
        with_arrows["arrow_down"] = down
        with_arrows["arrow_up"] = up
        base += "\n" + _ARROW_QSS.substitute(with_arrows)
    return base.strip()


# ---------------------------------------------------------------------------
# Bundled UI font (Inter — matches the Figma brand). Registered once from the
# packaged TTFs; the app font family is swapped to Inter while keeping the
# platform's default point size so layout metrics stay stable. Monospace
# widgets set their own Consolas font and are unaffected.
# ---------------------------------------------------------------------------

_UI_FONT_FAMILY: str | None = None  # "" = tried and unavailable; None = untried

# Base UI point size applied to the whole app (see apply_ui_font). 10pt reads
# more comfortably than Qt's cramped 9pt Windows default without risking the
# QFormLayout-starve / label-wrap layout bugs a larger jump would invite.
UI_FONT_POINT_SIZE = 10


def load_ui_font() -> str | None:
    """Register the bundled Inter TTFs once; return the family or ``None``.

    Idempotent — repeated calls reuse the first result. Returns ``None``
    when no Inter face could be registered (missing assets / unsupported),
    so callers can leave the platform default font in place.
    """
    global _UI_FONT_FAMILY
    if _UI_FONT_FAMILY is not None:
        return _UI_FONT_FAMILY or None

    from .paths import RESOURCES_DIR

    families: set[str] = set()
    fonts_dir = RESOURCES_DIR / "fonts"
    try:
        ttfs = sorted(fonts_dir.glob("Inter-*.ttf"))
    except OSError:
        ttfs = []
    for ttf in ttfs:
        fid = QFontDatabase.addApplicationFont(str(ttf))
        if fid != -1:
            families.update(QFontDatabase.applicationFontFamilies(fid))

    if "Inter" in families:
        _UI_FONT_FAMILY = "Inter"
    elif families:
        _UI_FONT_FAMILY = sorted(families)[0]
    else:
        _UI_FONT_FAMILY = ""
    return _UI_FONT_FAMILY or None


def apply_ui_font(app) -> None:
    """Swap to the bundled Inter family and bump the base point size.

    The family swap is a no-op when Inter is unavailable, but the size bump
    always applies. Windows' platform default is a cramped 9pt; ``UI_FONT_POINT_SIZE``
    is a touch larger and cascades to every widget that doesn't pin its own
    size — buttons, fields, tabs, labels, and the Consolas panes (they set
    only the family, so they inherit the size). Deliberate display sizes
    (splash / welcome wordmark, help body, runs-list) keep their own larger
    values. Call once at launch before the splash + main window are built.
    """
    font: QFont = app.font()
    family = load_ui_font()
    if family:
        font.setFamily(family)
    font.setPointSize(UI_FONT_POINT_SIZE)
    app.setFont(font)


def system_is_dark(app) -> bool:
    """True when the OS colour scheme is dark (Qt 6.5+). Defaults False."""
    try:
        from PySide6.QtCore import Qt
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except (AttributeError, RuntimeError):
        return False


def apply_theme(app, mode: ThemeMode | str) -> str:
    """Apply ``mode`` to the running app. Returns the effective key.

    Sets Fusion + the palette + the border/accent QSS, then re-appends
    the validators' invalid-field rule (a fresh ``setStyleSheet`` would
    otherwise drop it). Live-safe: call it again on a theme switch and
    every widget restyles.
    """
    effective = resolve_effective(mode, system_is_dark(app))
    spec = PALETTES[effective]
    app.setStyle("Fusion")
    app.setPalette(_build_qpalette(spec))
    app.setStyleSheet(_build_qss(spec))
    # Re-append the validators' global invalid-state rule onto the fresh
    # stylesheet (idempotent; no-op if somehow already present).
    from .views._validators import install_global_qss
    install_global_qss(app)
    return effective


def connect_system_theme(app, get_mode) -> None:
    """Re-apply the theme when the OS colour scheme changes.

    ``get_mode`` is a 0-arg callable returning the current saved
    :class:`ThemeMode`, so a later switch away from SYSTEM is honoured.
    Re-applying under a non-system mode is a harmless no-op.
    """
    try:
        app.styleHints().colorSchemeChanged.connect(
            lambda _scheme: apply_theme(app, get_mode())
        )
    except (AttributeError, RuntimeError):
        pass  # older Qt without the signal — system mode just won't live-update
