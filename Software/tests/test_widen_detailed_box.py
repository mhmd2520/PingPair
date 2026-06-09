"""Tests for ``widen_detailed_box`` — the 'Show Details…' readability fix.

Qt's default QMessageBox detail pane is a tiny, unreadable sliver. The helper
grows the detail QTextEdit and widens the dialog. These run headless via the
offscreen Qt platform forced in conftest.py.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QTextEdit

from pingpair.views._base import widen_detailed_box


@pytest.fixture(scope="module")
def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_widen_enlarges_detail_textedit(_app: QApplication) -> None:
    box = QMessageBox()
    box.setText("Something happened.")
    box.setDetailedText("line one\nline two\nline three")
    widen_detailed_box(box, min_width=720, detail_min_height=320)

    detail = box.findChild(QTextEdit)
    assert detail is not None
    assert detail.minimumWidth() >= 600
    assert detail.minimumHeight() >= 300


def test_widen_is_noop_safe_without_details(_app: QApplication) -> None:
    """No detailed text -> no QTextEdit -> must not raise."""
    box = QMessageBox()
    box.setText("No details here.")
    # Should return the box and not blow up.
    assert widen_detailed_box(box) is box


def test_widen_returns_same_box(_app: QApplication) -> None:
    box = QMessageBox()
    box.setDetailedText("x")
    assert widen_detailed_box(box) is box
