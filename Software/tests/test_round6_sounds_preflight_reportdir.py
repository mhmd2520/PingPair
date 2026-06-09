"""Round-6 VM-test fixes — Feature-6 updater + UX polish.

Covers four independent findings from Mohamed's Round-6 acceptance test:

* **#7** notification sounds (``core.sounds`` mapping + enable gate +
  best-effort no-op) and their QSettings persistence.
* **#4** the pre-download reachability probe (``updater.preflight_check``)
  and the About-tab "no internet" handler branch.
* **#6** the update-available dialog's single-row button order.
* **#8** report-dir leak fix: the default is stored as a blank sentinel and a
  leaked dev ``Software/Reports`` path is snapped beside the .exe when frozen.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from pingpair.core import sounds, updater
from pingpair.core.sounds import SoundEvent


# ---------------------------------------------------------------------------
# #7 — notification sounds (pure logic; injected beeper = no actual noise)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, tone",
    [
        (SoundEvent.SUCCESS, 0x40),  # MB_ICONASTERISK
        (SoundEvent.FAILURE, 0x10),  # MB_ICONHAND
        (SoundEvent.ERROR, 0x10),    # MB_ICONHAND
        (SoundEvent.PROMPT, 0x30),   # MB_ICONEXCLAMATION
    ],
)
def test_sound_event_maps_to_expected_tone(event, tone):
    rec: list[int] = []
    assert sounds.play(event, enabled=True, beeper=rec.append) is True
    assert rec == [tone]


def test_sound_disabled_is_silent():
    rec: list[int] = []
    assert sounds.play(SoundEvent.SUCCESS, enabled=False, beeper=rec.append) is False
    assert rec == []


def test_sound_best_effort_swallows_beeper_error():
    def boom(_tone: int) -> None:
        raise RuntimeError("no audio device")

    # A failing backend must never propagate — a beep can't break app flow.
    assert sounds.play(SoundEvent.ERROR, enabled=True, beeper=boom) is False


def test_sound_non_windows_no_backend_is_noop(monkeypatch):
    # On a non-Windows host with no injected beeper, play is a quiet no-op
    # (winsound is Windows-only).
    monkeypatch.setattr(sounds.sys, "platform", "linux")
    assert sounds.play(SoundEvent.SUCCESS, enabled=True) is False


def test_sounds_enabled_setting_roundtrip(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from pingpair import settings

    ini = tmp_path / "s.ini"
    monkeypatch.setattr(
        settings, "_q", lambda: QSettings(str(ini), QSettings.Format.IniFormat)
    )
    # Default on a fresh store is True.
    assert settings.load_sounds_enabled() is True
    settings.save_sounds_enabled(False)
    assert settings.load_sounds_enabled() is False
    settings.save_sounds_enabled(True)
    assert settings.load_sounds_enabled() is True


# ---------------------------------------------------------------------------
# #4 — pre-flight reachability probe
# ---------------------------------------------------------------------------


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_preflight_ok_when_github_answers():
    res = updater.preflight_check(opener=lambda req, timeout: _FakeResp())
    assert res.ok is True
    assert res.detail == ""


def test_preflight_ok_on_any_http_status():
    # An HTTP error status still means we reached GitHub — the route is fine.
    def http403(req, timeout):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    assert updater.preflight_check(opener=http403).ok is True


def test_preflight_offline_on_transport_failure():
    def no_route(req, timeout):
        raise urllib.error.URLError("network is unreachable")

    res = updater.preflight_check(opener=no_route)
    assert res.ok is False
    assert "No internet route" in res.detail


# ---------------------------------------------------------------------------
# Qt-dependent tests (#4 handler, #6 dialog). Skipped without PySide6.
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6", reason="GUI tests need Qt")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from pingpair.config import load_default_config  # noqa: E402
from pingpair.context import AppContext  # noqa: E402
from pingpair.views import about_view as av  # noqa: E402
from pingpair.views.about_view import (  # noqa: E402
    AboutView,
    _DownloadOutcome,
    _UpdateAvailableDialog,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_update_dialog_buttons_single_row_in_order(qapp):
    """#6: one row, left→right: Download & install, Show details, Don't remind."""
    dlg = _UpdateAvailableDialog(
        None,
        latest="0.2.0",
        current="0.1.0",
        can_install=True,
        release_notes="## What's new\n- stuff",
    )
    labels = [b.text() for b in dlg.findChildren(QPushButton)]
    assert labels == [
        "Download && install",  # && renders as a single & (mnemonic)
        "Show details",
        "Don't remind me again",
    ]


def test_update_dialog_no_notes_drops_show_details(qapp):
    dlg = _UpdateAvailableDialog(
        None, latest="0.2.0", current="0.1.0", can_install=True, release_notes=""
    )
    labels = [b.text() for b in dlg.findChildren(QPushButton)]
    assert "Show details" not in labels
    assert labels == ["Download && install", "Don't remind me again"]


def test_update_dialog_x_or_esc_dismisses(qapp):
    dlg = _UpdateAvailableDialog(
        None, latest="0.2.0", current="0.1.0", can_install=True, release_notes=""
    )
    # The title-bar X / Esc route through reject() → choice stays DISMISS.
    dlg.reject()
    assert dlg.choice == _UpdateAvailableDialog.DISMISS


def test_download_done_preflight_failure_shows_friendly_message(qapp, monkeypatch):
    """#4/#3: a pre-flight miss routes to the structured 'no internet' dialog,
    not a scary 'download failed', and clears the cache."""
    ctx = AppContext.create(load_default_config())
    about = AboutView(ctx)

    cleared: list[bool] = []
    monkeypatch.setattr(
        av.update_apply, "clear_update_cache", lambda *a, **k: cleared.append(True)
    )
    # _show_connection_error builds a modal QMessageBox; don't block the test.
    monkeypatch.setattr(av.QMessageBox, "exec", lambda self: 0)

    about._on_download_done(
        _DownloadOutcome(False, error="[Errno 11001] getaddrinfo failed",
                         preflight_failed=True)
    )

    assert cleared == [True]
    assert "No internet connection" in about._update_status.text()


def test_connection_help_is_plain_language(qapp):
    """#3: the help text gives a plain cause + fix, not a raw errno."""
    ctx = AppContext.create(load_default_config())
    about = AboutView(ctx)
    text = about._connection_help()
    assert "internet" in text.lower()
    assert "Check for updates" in text  # the concrete fix step
    assert "Errno" not in text  # raw technical detail stays out of the body


def test_connection_help_loopback_says_expected(qapp):
    from pingpair.context import Role

    ctx = AppContext.create(load_default_config())
    ctx.run_state.role = Role.LOOPBACK
    about = AboutView(ctx)
    text = about._connection_help()
    assert "Loopback" in text and "expected" in text


def test_update_available_dialog_plays_prompt_sound(qapp, monkeypatch):
    """#2: showing the update-available pop-up plays the PROMPT cue."""
    ctx = AppContext.create(load_default_config())
    about = AboutView(ctx)
    played: list = []
    monkeypatch.setattr(av, "notify_sound", lambda e: played.append(e))
    # Don't open the real modal.
    monkeypatch.setattr(av._UpdateAvailableDialog, "exec", lambda self: 0)

    res = updater.UpdateCheckResult(
        status=updater.UpdateStatus.UPDATE_AVAILABLE,
        current_version="0.1.0",
        latest_version="0.2.0",
    )
    about._show_update_available_dialog(res)
    assert av.SoundEvent.PROMPT in played


# ---------------------------------------------------------------------------
# #8 — report-dir leak fix (blank-sentinel-when-default + frozen migration)
# ---------------------------------------------------------------------------


def _iso_settings(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from pingpair import settings

    ini = tmp_path / "rs.ini"
    monkeypatch.setattr(
        settings, "_q", lambda: QSettings(str(ini), QSettings.Format.IniFormat)
    )
    return settings


def test_default_report_dir_saved_blank(tmp_path, monkeypatch):
    """A report_dir equal to REPORTS_DIR is stored blank, so the default
    re-resolves per environment on load (no dev/frozen cross-pollution)."""
    settings = _iso_settings(tmp_path, monkeypatch)
    from pingpair.context import RunState

    rs = RunState()
    rs.report_dir = settings.REPORTS_DIR  # the computed default
    settings.save_from(rs)
    assert settings._q().value("report/dir") == ""


def test_custom_report_dir_saved_verbatim(tmp_path, monkeypatch):
    settings = _iso_settings(tmp_path, monkeypatch)
    from pingpair.context import RunState

    rs = RunState()
    custom = Path("X:/MyReports")
    rs.report_dir = custom
    settings.save_from(rs)
    # Stored verbatim as the platform-native string (Path normalises / → \).
    assert settings._q().value("report/dir") == str(custom)


def test_frozen_preserves_custom_report_dir(tmp_path, monkeypatch):
    settings = _iso_settings(tmp_path, monkeypatch)
    from pingpair.context import RunState

    monkeypatch.setattr(settings, "REPORTS_DIR", tmp_path / "install" / "Reports")
    monkeypatch.setattr(settings.sys, "frozen", True, raising=False)

    seed = settings._q()
    seed.setValue("report/dir", "X:/Operator/Chosen")
    seed.sync()

    rs = RunState()
    settings.load_into(rs)
    assert rs.report_dir == Path("X:/Operator/Chosen")
