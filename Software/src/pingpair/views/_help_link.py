"""Shared helper: an error dialog with a one-click route into the guide.

Feature 8 follow-up — instead of a dead-end error popup, this shows the same
critical message with an extra "Open Help → …" button that fires the
:attr:`AppContext.open_help` hook (wired by :class:`pingpair.app.MainWindow`)
to jump straight to the relevant guide section. Degrades to a plain error box
when no navigation hook is wired (e.g. headless tests), so callers never branch.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from ..context import AppContext


def show_error_with_help(
    parent: QWidget,
    ctx: AppContext,
    title: str,
    text: str,
    *,
    help_key: str = "troubleshooting",
    help_label: str = "Open Help → Troubleshooting",
) -> None:
    """Show a critical error box; add a button that opens guide ``help_key``."""
    opener = ctx.open_help
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(text)
    box.addButton(QMessageBox.StandardButton.Ok)
    help_btn = (
        box.addButton(help_label, QMessageBox.ButtonRole.ActionRole)
        if opener is not None
        else None
    )
    box.exec()
    if opener is not None and help_btn is not None and box.clickedButton() is help_btn:
        opener(help_key)
