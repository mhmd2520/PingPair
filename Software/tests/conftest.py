"""Shared pytest setup.

Force Qt's offscreen platform plugin so GUI-widget regression tests
(e.g. ``test_round19_*``) can build real ``QWidget``s without a display.
Set at collection-import time — before any test creates a
``QApplication`` — and via ``setdefault`` so an explicit
``QT_QPA_PLATFORM`` in the environment still wins. The existing
QSettings tests only build a ``QCoreApplication`` (no platform plugin),
so they are unaffected.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(autouse=True)
def _silence_view_sounds(monkeypatch):
    """Keep notification sounds (Round-6 #7 / Round-7 #2) silent + deterministic
    in tests.

    The view helper ``views._sounds.notify_sound`` resolves ``play`` from its
    module globals at call time, so patching ``_sounds.play`` to a no-op
    silences every view-level beep (Run-tab sweep events, About-tab update
    pop-ups) without touching ``core.sounds.play`` — which the dedicated sound
    tests exercise directly with an injected beeper.

    Gated on ``_sounds`` already being imported: a test module that uses a view
    imports it at collection time, so it's present before any view test runs;
    a pure-logic test run that never touches Qt leaves it absent and stays
    Qt-free (importing it here would eagerly pull in the whole ``views``
    package + PySide6)."""
    sounds_mod = sys.modules.get("pingpair.views._sounds")
    if sounds_mod is not None:
        monkeypatch.setattr(sounds_mod, "play", lambda *a, **k: False, raising=False)


@pytest.fixture(autouse=True)
def _no_live_wifi_subnet_probe(monkeypatch):
    """Stop the Run tab from probing the HOST's live Wi-Fi state in tests.

    ``_ClientPanel._on_run`` (and ``_ServerPanel._warn_if_wifi_on_test_subnet``)
    call ``wifi_adapters_on_test_subnet(cfg)`` — a real ``psutil`` read of this
    machine's adapters. On a dev/CI box whose Wi-Fi happens to share the test
    subnet (``192.168.1.x``) that returns a conflict, and ``_on_run`` then pops
    a **modal** ``QMessageBox.critical`` that hangs forever under the offscreen
    platform (no one to dismiss it) — which deadlocked the full suite at
    ``test_round20``. Default every test to "no conflict" so behaviour never
    depends on the host's live Wi-Fi; the block itself is covered by the pure
    ``test_wifi_off`` tests, and a test that wants the block can re-patch this.

    Gated on ``script_view`` already being imported so a pure-logic run stays
    Qt-free (same pattern as ``_silence_view_sounds``)."""
    mod = sys.modules.get("pingpair.views.script_view")
    if mod is not None:
        monkeypatch.setattr(
            mod, "wifi_adapters_on_test_subnet", lambda _cfg: [], raising=False
        )
