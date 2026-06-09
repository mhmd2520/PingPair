"""Shared base class for tab pages.

Every view receives the :class:`AppContext` so it can read config and log,
and every view exposes a uniform ``refresh()`` slot the main window can
call when the active tab changes.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QSpacerItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..context import AppContext

# Minimum vertical size for QLineEdit / QSpinBox inputs so form rows
# can't collapse to a few pixels when a sibling (raw-JSON pane, recent-
# reports list, …) claims the vertical stretch. Calibrated on the user's
# 1920x1080 Windows VM — 24 px fits the text + caret without looking
# oversized. Shared by the Config and Save Options forms.
_INPUT_MIN_HEIGHT_PX = 24


def _shape_input(widget: QWidget) -> QWidget:
    """Apply the standard min-height + Fixed-vertical size policy.

    Without this, QLineEdit / QSpinBox accept arbitrary vertical
    compression and end up rendering at ~5 px tall when the parent
    layout's other siblings have ``stretch=1``. Calling this on every
    form input keeps every row at the same visual height regardless of
    window size.
    """
    widget.setMinimumHeight(_INPUT_MIN_HEIGHT_PX)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return widget


def widen_detailed_box(
    box: QMessageBox,
    *,
    min_width: int = 720,
    detail_min_height: int = 320,
) -> QMessageBox:
    """Give a ``QMessageBox`` 'Show Details…' pane a readable size.

    Qt's default detail ``QTextEdit`` is a cramped ~50x70 px box that squeezes
    multi-line output (netsh transcripts, saved-file lists, the command
    preview) into an unreadable sliver — identically broken on Light and Dark.
    This:

    * widens the whole dialog via the classic ``QGridLayout`` spacer trick
      (QMessageBox lays its widgets out in a grid), and
    * gives the detail ``QTextEdit`` a sane minimum size so the expanded pane
      shows several lines at full width.

    Call it **after** ``setDetailedText(...)`` (the detail widget only exists
    once detail text is set) and **before** ``exec()``. Theme-safe — it only
    touches sizes / layout, never colours, so :mod:`theme` styling is intact.
    Returns the same box so callers can chain. No-op-safe if no detail text
    was set (the ``QTextEdit`` lookup just returns ``None``).
    """
    detail = box.findChild(QTextEdit)
    if detail is not None:
        detail.setMinimumSize(QSize(min_width - 48, detail_min_height))
        detail.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
    layout = box.layout()
    if isinstance(layout, QGridLayout):
        # A zero-height, fixed-width spacer in a new full-span row forces the
        # dialog's minimum width without disturbing the existing rows.
        spacer = QSpacerItem(
            min_width, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding
        )
        layout.addItem(spacer, layout.rowCount(), 0, 1, layout.columnCount())
    return box


class BaseView(QWidget):
    """Common parent for all tab pages."""

    title: str = "Untitled"
    todo: str = ""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._build_placeholder()

    def _build_placeholder(self) -> None:
        """Render a 'coming soon' card. Subclasses override entirely."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel(f"<h2>{self.title}</h2>")
        layout.addWidget(title)

        if self.todo:
            todo = QLabel(self.todo)
            todo.setWordWrap(True)
            todo.setStyleSheet("color: #888;")
            layout.addWidget(todo)

        layout.addStretch(1)

    def refresh(self) -> None:
        """Re-read state from ctx. Default: no-op."""
