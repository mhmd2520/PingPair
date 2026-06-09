"""Real-event interaction tests driven by pytest-qt's ``qtbot``.

Most of the suite builds widgets and calls their methods directly. These tests
go one layer deeper: they post **actual mouse / keyboard events** through the Qt
event loop and assert on the signals that fall out, the way a user's click does.

Two reasons this layer earns its keep:

* It exercises the cell-click forwarding in :class:`_CheckCell` — a real
  ``mousePressEvent`` override that a method-level call would bypass entirely.
* ``qtbot`` installs an exception hook that **fails the test if any Qt slot or
  virtual method raises** during the interaction (a plain ``QApplication``
  would swallow it to stderr). That is the precise silent-crash class behind
  Round-21 YY (``QThread: Destroyed while still running``), so wiring qtbot in
  gives every interaction test a free guard against it.

The offscreen platform is forced by ``conftest.py``; ``qt_api = "pyside6"`` in
``pyproject.toml`` pins qtbot to the binding the app ships.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="qtbot interaction tests need Qt")
pytestqt = pytest.importorskip("pytestqt", reason="needs the pytest-qt plugin")

from pingpair.config import load_default_config  # noqa: E402
from pingpair.views._sweep_table import _COL_RUN, SweepTable  # noqa: E402


def _build_table(qtbot) -> SweepTable:
    table = SweepTable(load_default_config())
    qtbot.addWidget(table)  # qtbot owns teardown + watches for slot exceptions
    return table


def _run_cell(table: SweepTable, row: int):
    """The centred-checkbox cell widget in the Run column for ``row``."""
    return table.cellWidget(row, _COL_RUN)


def test_clicking_a_run_cell_toggles_and_emits(qtbot) -> None:
    from PySide6.QtCore import Qt

    table = _build_table(qtbot)
    assert table.selected_count() == table.total_count(), "rows start all-checked"

    cell = _run_cell(table, 0)
    # blocker captures the selection_changed emission caused by the click.
    with qtbot.waitSignal(table.selection_changed, timeout=1000) as blocker:
        qtbot.mouseClick(cell, Qt.MouseButton.LeftButton)

    assert blocker.args == [table.total_count() - 1], "emits the new checked count"
    assert table.selected_count() == table.total_count() - 1
    assert 1 not in table.selected_case_indexes(), "case #1 is now unchecked"


def test_clicking_beside_the_box_still_toggles(qtbot) -> None:
    """_CheckCell forwards a click anywhere in the cell to the checkbox.

    Click the top-left corner (pos=(1, 1)) — beside the centred 16 px box — and
    the row must still flip. This is the behaviour the override exists for.
    """
    from PySide6.QtCore import QPoint, Qt

    table = _build_table(qtbot)
    cell = _run_cell(table, 2)

    with qtbot.waitSignal(table.selection_changed, timeout=1000):
        qtbot.mouseClick(cell, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))

    assert 3 not in table.selected_case_indexes(), "corner click unchecked case #3"


def test_repeated_clicks_round_trip_without_slot_errors(qtbot) -> None:
    """Toggle one row off and back on via real clicks.

    The assertions confirm the state round-trips; the real value is implicit —
    if any selection_changed slot raised mid-toggle, qtbot would fail the test.
    """
    from PySide6.QtCore import Qt

    table = _build_table(qtbot)
    full = table.total_count()
    cell = _run_cell(table, 0)

    qtbot.mouseClick(cell, Qt.MouseButton.LeftButton)
    assert table.selected_count() == full - 1
    qtbot.mouseClick(cell, Qt.MouseButton.LeftButton)
    assert table.selected_count() == full
