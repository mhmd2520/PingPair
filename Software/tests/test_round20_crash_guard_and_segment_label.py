"""Round-20 VM-test fixes — WW (crash guard) and XX (current-segment label).

Reported by Mohamed after a VM session of repeatedly starting and stopping
sweeps in Loopback / Client mode.

**WW — the app *suddenly* vanished after rapid Run/Stop churn.** PySide6
routes an unhandled exception that escapes a Qt slot (the queued
``_on_event`` / ``_on_sweep_finished`` / ``_on_progress_tick`` handlers,
hammered by repeated start/stop) through ``sys.excepthook`` — and with the
*default* hook it then terminates the process with no traceback anywhere.
``app.install_crash_guard`` overrides ``sys.excepthook`` /
``threading.excepthook`` so the traceback is written to the PingPair log
and the event loop survives. These tests pin that the hooks are installed
and that they log (instead of re-raising / aborting).

**XX — the Continuous-mode label field stayed frozen on segment 1.** The
Run-tab field was only ever segment 1's label; as the operator advanced
through segments it kept showing the first one, which read as a stuck
value. The field is now a live read-out of the *current* segment (caption
renamed "Current segment label:"), restored to the operator's original
entry when the run ends.

WW tests need only ``app.install_crash_guard`` (Qt imported lazily inside
the dialog branch, which is patched out here). XX tests build a real
``_ClientPanel`` under the offscreen Qt platform (see ``conftest.py``).
"""

from __future__ import annotations

import logging
import sys
import threading
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6", reason="Round-20 needs Qt")
pytest.importorskip("pyqtgraph", reason="_ClientPanel builds pyqtgraph plots")

from pingpair.app import install_crash_guard
from pingpair.config import load_default_config
from pingpair.context import AppContext, Role, RunState

# ===========================================================================
# WW — crash guard converts an unhandled slot/thread exception into a log.
# ===========================================================================


@pytest.fixture
def restore_hooks():
    """Save + restore the global excepthooks so the guard can't leak into
    other tests (it would otherwise swallow their Qt-slot exceptions)."""
    orig_sys, orig_thread = sys.excepthook, threading.excepthook
    try:
        yield
    finally:
        sys.excepthook = orig_sys
        threading.excepthook = orig_thread


@pytest.fixture
def _no_blocking_dialog(monkeypatch):
    """Neutralise the guard's modal QMessageBox so it can't hang the suite
    under the offscreen platform (no user to dismiss it)."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)


def test_install_crash_guard_overrides_both_hooks(restore_hooks) -> None:
    orig_sys, orig_thread = sys.excepthook, threading.excepthook
    install_crash_guard(logging.getLogger("test-ww-install"))
    assert sys.excepthook is not orig_sys, "sys.excepthook must be overridden"
    assert threading.excepthook is not orig_thread, (
        "threading.excepthook must be overridden"
    )


def test_crash_guard_logs_and_does_not_reraise(
    restore_hooks, _no_blocking_dialog
) -> None:
    """The hook PySide6 calls on an escaped slot exception must log the
    traceback and return normally (so the event loop keeps running)."""
    logger = MagicMock()
    install_crash_guard(logger)
    try:
        raise ValueError("boom-during-sweep")
    except ValueError:
        # This is exactly what PySide6 does when a slot raises.
        sys.excepthook(*sys.exc_info())  # must NOT propagate

    assert logger.error.called, "the unhandled exception must be logged"
    logged_detail = logger.error.call_args[0][1]
    assert "boom-during-sweep" in logged_detail, (
        "the full traceback (incl. the message) must reach the log"
    )


def test_crash_guard_thread_hook_logs_and_ignores_keyboard_interrupt(
    restore_hooks, _no_blocking_dialog
) -> None:
    logger = MagicMock()
    install_crash_guard(logger)

    try:
        raise RuntimeError("thread-boom")
    except RuntimeError as exc:
        args = threading.ExceptHookArgs(
            (type(exc), exc, exc.__traceback__, None)
        )
    threading.excepthook(args)
    assert logger.error.called, "a background-thread exception must be logged"

    # A KeyboardInterrupt in a thread is shutdown, not a crash — ignore it
    # exactly as the default threading.excepthook does.
    logger.reset_mock()
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt as exc:
        ki_args = threading.ExceptHookArgs(
            (type(exc), exc, exc.__traceback__, None)
        )
    threading.excepthook(ki_args)
    assert not logger.error.called, "KeyboardInterrupt must be ignored"


def test_crash_guard_does_not_build_dialog_off_gui_thread(
    qapp, restore_hooks, monkeypatch
) -> None:
    """A QMessageBox is a QWidget — building it on a worker thread is itself
    a Qt cross-thread fatal. When the guard fires off the GUI thread it must
    log only, never touch a widget (the dialog is a GUI-thread-only path)."""
    from PySide6.QtWidgets import QMessageBox

    dialog_calls: list[int] = []
    monkeypatch.setattr(
        QMessageBox, "critical", lambda *a, **k: dialog_calls.append(1)
    )
    logger = MagicMock()
    install_crash_guard(logger)

    def worker() -> None:
        try:
            raise RuntimeError("off-gui-thread-boom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert logger.error.called, "the worker-thread fault must still be logged"
    assert not dialog_calls, (
        "no QMessageBox may be built off the GUI thread (the cross-thread "
        "fatal the guard exists to prevent)"
    )


# ===========================================================================
# XX — Continuous-mode label field tracks the current segment (real panel).
# ===========================================================================


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _build_client_panel(qapp, *, role: Role = Role.CLIENT, loopback: bool = False):
    from pingpair.views.script_view import _ClientPanel

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-round20"),
        run_state=RunState(role=role),
    )
    return _ClientPanel(ctx, loopback=loopback)


def test_segment_label_caption_renamed(qapp) -> None:
    """The caption now says 'Current', not 'First'."""
    panel = _build_client_panel(qapp)
    assert panel._segment_label_caption.text() == "Current segment label:"


def test_on_run_continuous_captures_and_displays_first_segment(qapp) -> None:
    """Starting a continuous run captures the typed text and shows the
    resolved segment-1 label in the field (without spawning a worker)."""
    panel = _build_client_panel(qapp)
    panel._sweep_table.set_selected_case_indexes([1])
    panel.ctx.run_state.continuous_mode = True
    panel._segment_label_edit.setText("Cab M2")

    started = {}
    panel._start_sweep_worker = lambda sel: started.setdefault("sel", sel)
    panel._on_run()

    assert started, "the sweep kickoff must still be requested"
    assert panel._first_segment_label_text == "Cab M2", (
        "the operator's original entry must be stashed for later restore"
    )
    assert panel._current_segment_label == "Cab M2"
    assert panel._segment_label_edit.text() == "Cab M2", (
        "the field must show the segment now in flight"
    )


def test_segment_label_field_tracks_advancing_segment(qapp) -> None:
    """Advancing (Continue/Retry/Skip all funnel through _kickoff_next_segment)
    updates the field to the segment now starting — the XX regression was it
    staying frozen on segment 1."""
    panel = _build_client_panel(qapp)
    panel._active_subset = [1]
    panel._current_segment_label = "Segment 3 (Cab X)"
    panel._segment_label_edit.setText("Segment 1")  # the stale, stuck value

    panel._start_sweep_worker = lambda _sel: None
    panel._kickoff_next_segment()

    assert panel._segment_label_edit.text() == "Segment 3 (Cab X)", (
        "the field must follow the advancing segment, not stay on segment 1"
    )


def test_segment_label_restored_after_zero_case_finish(qapp) -> None:
    """When the run ends, the field returns to the operator's original
    first-segment entry rather than the last segment's name."""
    panel = _build_client_panel(qapp)
    panel._first_segment_label_text = "Cab A -> B"
    panel._segment_label_edit.setText("Segment 5 (last)")
    panel._worker = None

    panel._reset_after_zero_case_finish()

    assert panel._segment_label_edit.text() == "Cab A -> B", (
        "the field must be restored to the operator's segment-1 entry"
    )
