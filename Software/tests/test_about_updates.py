"""Tests for the About-tab update-check wiring (Feature 6 gating logic).

Exercises the result-routing and auto-check gating directly, monkeypatching
the dialog methods and settings helpers so nothing touches the network, a
real QThread, or modal ``exec()``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="AboutView is a Qt widget")

from PySide6.QtWidgets import QApplication

from pingpair.config import load_default_config
from pingpair.context import AppContext, Role
from pingpair.core import update_apply
from pingpair.core.updater import ReleaseAsset, UpdateCheckResult, UpdateStatus
from pingpair.views import about_view as av
from pingpair.views.about_view import AboutView, _DownloadOutcome


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def about(qapp):
    ctx = AppContext.create(load_default_config())
    return AboutView(ctx)


def _result(status: UpdateStatus, **kw) -> UpdateCheckResult:
    base = dict(status=status, current_version="0.1.0")
    base.update(kw)
    return UpdateCheckResult(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# maybe_auto_check gating
# --------------------------------------------------------------------------


def _stub_start(about, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(
        about, "_start_check", lambda *, manual: calls.append(manual)
    )
    return calls


def test_auto_check_skipped_when_opted_out(about, monkeypatch):
    calls = _stub_start(about, monkeypatch)
    monkeypatch.setattr(av.settings, "load_updates_auto_check", lambda: False)
    about.maybe_auto_check()
    assert calls == []


def test_auto_check_runs_in_loopback(about, monkeypatch):
    # Loopback no longer skips the launch auto-check (2026-06-02): a dev box
    # usually has internet at launch and the launch check is wanted for
    # update-flow testing (the manual button already worked in Loopback).
    calls = _stub_start(about, monkeypatch)
    monkeypatch.setattr(av.settings, "load_updates_auto_check", lambda: True)
    monkeypatch.setattr(av.settings, "load_updates_last_check_ts", lambda: 0.0)
    monkeypatch.setattr(av.settings, "save_updates_last_check_ts", lambda ts: None)
    monkeypatch.setattr(av.time, "time", lambda: 1_700_000_000.0)
    about.ctx.run_state.role = Role.LOOPBACK
    about.maybe_auto_check()
    assert calls == [False]


def test_auto_check_runs_every_launch_no_throttle(about, monkeypatch):
    # The 24h throttle was removed (#2): the auto-check fires on every launch
    # so the user is reminded of a waiting update each time.
    calls = _stub_start(about, monkeypatch)
    monkeypatch.setattr(av.settings, "load_updates_auto_check", lambda: True)
    about.ctx.run_state.role = Role.CLIENT
    monkeypatch.setattr(av.time, "time", lambda: 1000.0)
    monkeypatch.setattr(av.settings, "load_updates_last_check_ts", lambda: 999.0)
    monkeypatch.setattr(av.settings, "save_updates_last_check_ts", lambda ts: None)
    about.maybe_auto_check()
    assert calls == [False]  # ran despite a 1s-ago last check


def test_auto_check_runs_and_records_timestamp(about, monkeypatch):
    calls = _stub_start(about, monkeypatch)
    saved: list[float] = []
    monkeypatch.setattr(av.settings, "load_updates_auto_check", lambda: True)
    # Never checked (0.0) vs a realistic epoch clock — well past the 24 h
    # window, so the throttle lets it run.
    monkeypatch.setattr(av.settings, "load_updates_last_check_ts", lambda: 0.0)
    monkeypatch.setattr(av.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(
        av.settings, "save_updates_last_check_ts", lambda ts: saved.append(ts)
    )
    about.ctx.run_state.role = Role.SERVER
    about.maybe_auto_check()
    assert calls == [False]  # manual=False
    assert saved == [1_700_000_000.0]


# --------------------------------------------------------------------------
# _on_check_done routing
# --------------------------------------------------------------------------


def _stub_dialogs(about, monkeypatch):
    seen = {"manual": 0, "available": 0}
    monkeypatch.setattr(
        about, "_show_manual_result", lambda r: seen.__setitem__("manual", seen["manual"] + 1)
    )
    monkeypatch.setattr(
        about,
        "_show_update_available_dialog",
        lambda r: seen.__setitem__("available", seen["available"] + 1),
    )
    return seen


def test_manual_check_always_shows_dialog(about, monkeypatch):
    seen = _stub_dialogs(about, monkeypatch)
    about._manual_check = True
    about._on_check_done(_result(UpdateStatus.UP_TO_DATE, latest_version="0.1.0"))
    assert seen["manual"] == 1


def test_auto_update_pops_modal_every_launch(about, monkeypatch):
    # #2: the modal now appears on EVERY auto-check while an update exists
    # (the user opts out via the modal's "Don't remind me again"), not just
    # once per version.
    seen = _stub_dialogs(about, monkeypatch)
    about._manual_check = False
    res = _result(
        UpdateStatus.UPDATE_AVAILABLE, latest_version="0.2.0", release_url="https://x"
    )
    about._on_check_done(res)
    about._on_check_done(res)
    assert seen["available"] == 2  # both launches showed it


def test_auto_up_to_date_shows_no_dialog(about, monkeypatch):
    seen = _stub_dialogs(about, monkeypatch)
    about._manual_check = False
    about._on_check_done(_result(UpdateStatus.UP_TO_DATE, latest_version="0.1.0"))
    assert seen == {"manual": 0, "available": 0}


# --------------------------------------------------------------------------
# status-line rendering
# --------------------------------------------------------------------------


def test_status_line_update_available_has_link(about):
    about._set_update_status_from_result(
        _result(UpdateStatus.UPDATE_AVAILABLE, latest_version="0.2.0", release_url="https://r")
    )
    # isVisible() is False for a widget whose top-level was never shown;
    # assert on the explicit shown flag the code sets via setVisible(True).
    assert not about._update_status.isHidden()
    text = about._update_status.text()
    assert "0.2.0" in text and "https://r" in text


def test_status_line_up_to_date(about):
    about._set_update_status_from_result(_result(UpdateStatus.UP_TO_DATE))
    assert "up to date" in about._update_status.text().lower()


def test_status_line_error(about):
    about._set_update_status_from_result(_result(UpdateStatus.ERROR, detail="x"))
    assert "GitHub" in about._update_status.text()


def test_auto_check_toggle_persists(about, monkeypatch):
    saved: list[bool] = []
    monkeypatch.setattr(av.settings, "save_updates_auto_check", lambda v: saved.append(v))
    about._auto_check.setChecked(False)
    about._auto_check.setChecked(True)
    assert saved[-1] is True


# --------------------------------------------------------------------------
# update-available dialog (#1: working X / Esc dismiss)
# --------------------------------------------------------------------------


def test_update_dialog_defaults_to_dismiss(qapp):
    # X / Esc (reject) must leave choice == DISMISS so closing the popup does
    # nothing — the bug was QMessageBox's X being disabled with no Later button.
    dlg = av._UpdateAvailableDialog(
        None, latest="0.2.0", current="0.1.0", can_install=True, release_notes=""
    )
    assert dlg.choice == av._UpdateAvailableDialog.DISMISS
    dlg.reject()  # simulates the title-bar X / Esc
    assert dlg.choice == av._UpdateAvailableDialog.DISMISS


def test_update_dialog_install_and_dont_remind_choices(qapp):
    dlg = av._UpdateAvailableDialog(
        None, latest="0.2.0", current="0.1.0", can_install=True, release_notes="x"
    )
    dlg._on_install()
    assert dlg.choice == av._UpdateAvailableDialog.INSTALL

    dlg2 = av._UpdateAvailableDialog(
        None, latest="0.2.0", current="0.1.0", can_install=True, release_notes=""
    )
    dlg2._on_dont_remind()
    assert dlg2.choice == av._UpdateAvailableDialog.DONT_REMIND


# --------------------------------------------------------------------------
# download ETA formatting (#3: minutes + seconds for >= 60s)
# --------------------------------------------------------------------------


def test_fmt_eta_seconds_under_a_minute():
    assert av._fmt_eta(0) == "0s"
    assert av._fmt_eta(45) == "45s"
    assert av._fmt_eta(59) == "59s"


def test_fmt_eta_minutes_and_seconds_over_a_minute():
    assert av._fmt_eta(60) == "1m 0s"
    assert av._fmt_eta(65) == "1m 5s"
    assert av._fmt_eta(901) == "15m 1s"


# --------------------------------------------------------------------------
# install button visibility (frozen vs source) + download routing
# --------------------------------------------------------------------------


def _installable(**kw) -> UpdateCheckResult:
    base = dict(
        status=UpdateStatus.UPDATE_AVAILABLE,
        current_version="0.1.0",
        latest_version="0.2.0",
        asset=ReleaseAsset(
            name="PingPair-0.2.0-win64.zip", url="https://x/b.zip", size=1
        ),
    )
    base.update(kw)
    return UpdateCheckResult(**base)  # type: ignore[arg-type]


def test_install_button_hidden_on_source_install(about, monkeypatch):
    _stub_dialogs(about, monkeypatch)
    monkeypatch.setattr(av.update_apply, "is_frozen", lambda: False)
    about._manual_check = False
    about._on_check_done(_installable())
    assert about._install_btn.isHidden()
    assert about._pending_update is None


def test_install_button_shown_when_frozen_and_installable(about, monkeypatch):
    _stub_dialogs(about, monkeypatch)
    monkeypatch.setattr(av.update_apply, "is_frozen", lambda: True)
    about._manual_check = False
    res = _installable()
    about._on_check_done(res)
    assert not about._install_btn.isHidden()
    assert about._pending_update is res


def test_download_done_success_triggers_apply(about, monkeypatch):
    applied: list = []
    monkeypatch.setattr(about, "_apply_and_restart", lambda p: applied.append(p))
    about._on_download_done(_DownloadOutcome(True, path=av.Path("bundle.zip")))
    assert applied == [av.Path("bundle.zip")]
    assert about._install_btn.isEnabled()


def test_download_done_failure_does_not_apply(about, monkeypatch):
    applied: list = []
    monkeypatch.setattr(about, "_apply_and_restart", lambda p: applied.append(p))
    shown: list = []
    monkeypatch.setattr(av.QMessageBox, "warning", lambda *a, **k: shown.append(a))
    cleaned: list = []
    monkeypatch.setattr(av.update_apply, "clear_update_cache", lambda **k: cleaned.append(1))
    about._on_download_done(_DownloadOutcome(False, error="boom"))
    assert applied == []
    assert shown  # a warning was surfaced
    assert "failed" in about._update_status.text().lower()
    assert cleaned  # failed download auto-cleaned the cache


def test_download_done_cancel_cleans_cache_no_apply(about, monkeypatch):
    applied: list = []
    monkeypatch.setattr(about, "_apply_and_restart", lambda p: applied.append(p))
    cleaned: list = []
    monkeypatch.setattr(av.update_apply, "clear_update_cache", lambda **k: cleaned.append(1))
    about._on_download_done(_DownloadOutcome(False, cancelled=True))
    assert applied == []
    assert cleaned  # cancel auto-cleaned the partial download
    assert "cancel" in about._update_status.text().lower()


def test_apply_and_restart_source_error_is_handled(about, monkeypatch):
    def _raise(_p):
        raise update_apply.SourceInstallError("from source")

    monkeypatch.setattr(av.update_apply, "apply_update", _raise)
    info: list = []
    monkeypatch.setattr(av.QMessageBox, "information", lambda *a, **k: info.append(a))
    quit_called: list = []
    monkeypatch.setattr(
        av.QApplication, "quit", staticmethod(lambda: quit_called.append(True))
    )
    about._apply_and_restart(av.Path("bundle.zip"))
    assert info  # told the user it's a source install
    assert quit_called == []  # did NOT quit/relaunch
