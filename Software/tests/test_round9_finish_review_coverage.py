"""Round-9 /finish close-out — coverage the phase review flagged as thin.

The Feature-6 phase review (pr-test-analyzer) found the role-aware branches of
``AboutView._connection_help`` (Wi-Fi-off, Server/Client point-to-point) and the
``preflight_check`` timeout path untested. These guards close those gaps so the
user-facing offline help text (Round-7 #3) and the "fast offline, no 60 s hang"
probe (Round-6 #4) can't silently regress.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="AboutView is a Qt widget")

from PySide6.QtWidgets import QApplication

from pingpair.config import load_default_config
from pingpair.context import AppContext, Role
from pingpair.core import updater
from pingpair.views.about_view import AboutView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_connection_help_wifi_off_branch(qapp):
    """When Wi-Fi was disabled for testing, the help calls that out + the fix."""
    ctx = AppContext.create(load_default_config())
    ctx.run_state.role = Role.CLIENT
    ctx.run_state.wifi_offline_adapter = "Wi-Fi"
    text = AboutView(ctx)._connection_help()
    assert "Wi-Fi" in text
    assert "turned off" in text.lower()
    # The Wi-Fi-off note takes priority over the point-to-point tip.
    assert "direct Ethernet" not in text


def test_connection_help_point_to_point_branch(qapp):
    """A Server/Client role (Wi-Fi still on) gets the direct-link tip."""
    ctx = AppContext.create(load_default_config())
    ctx.run_state.role = Role.SERVER
    ctx.run_state.wifi_offline_adapter = None
    text = AboutView(ctx)._connection_help()
    assert "direct Ethernet" in text
    assert "reconnect to your normal network" in text


def test_preflight_offline_on_timeout(qapp):
    """A socket timeout is treated as offline (the named motivation for #4)."""

    def times_out(req, timeout):
        raise TimeoutError("timed out")

    res = updater.preflight_check(opener=times_out)
    assert res.ok is False
    assert res.detail  # a non-empty reason for the friendly dialog


# ---------------------------------------------------------------------------
# SEC-001 (security audit) — the unsigned bundle's SHA-256 is the ONLY
# integrity gate, so the download worker must REQUIRE it and refuse non-HTTPS.
# ---------------------------------------------------------------------------


def _run_worker_outcome(qapp, tmp_path, *, asset_url, sha256_url):
    from pingpair.views.about_view import _UpdateDownloadWorker

    result = updater.UpdateCheckResult(
        status=updater.UpdateStatus.UPDATE_AVAILABLE,
        current_version="0.1.0",
        latest_version="0.2.0",
        asset=updater.ReleaseAsset(name="PingPair.zip", url=asset_url, size=10),
        sha256_url=sha256_url,
    )
    worker = _UpdateDownloadWorker(result, tmp_path)
    captured: list = []
    worker.done.connect(lambda o: captured.append(o))
    worker.run()  # synchronous (not start()) — emits done in this thread
    return captured[-1]


def test_update_refused_when_checksum_missing(qapp, tmp_path):
    """A release with no .sha256 is refused, not installed unverified."""
    outcome = _run_worker_outcome(
        qapp, tmp_path, asset_url="https://example.test/PingPair.zip", sha256_url=""
    )
    assert outcome.ok is False
    assert "checksum" in outcome.error.lower()


def test_update_refused_on_non_https_asset(qapp, tmp_path):
    """A downgraded http:// bundle URL is refused before any download."""
    outcome = _run_worker_outcome(
        qapp,
        tmp_path,
        asset_url="http://example.test/PingPair.zip",
        sha256_url="https://example.test/PingPair.zip.sha256",
    )
    assert outcome.ok is False
    assert "https" in outcome.error.lower()


# ---------------------------------------------------------------------------
# Architecture-critic root-cause fix — dev and frozen must NOT share a
# QSettings store (the source of the Round-6 report-dir leak).
# ---------------------------------------------------------------------------


def test_dev_and_frozen_use_separate_settings_stores():
    from pingpair import settings

    assert settings._org_for_env(frozen=True) == "PingPair"
    assert settings._org_for_env(frozen=False) != settings._org_for_env(
        frozen=True
    )


# ---------------------------------------------------------------------------
# SEC-002 — the elevated update-swap must lock its staging area against a
# non-admin TOCTOU tamper. The icacls call is best-effort and never raises.
# ---------------------------------------------------------------------------


def test_restrict_to_admins_noop_off_windows(monkeypatch, tmp_path):
    from pingpair.core import update_apply

    monkeypatch.setattr(update_apply.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: called.append(a)
    )
    update_apply._restrict_to_admins(tmp_path)
    assert called == []  # never shells out off-Windows


def test_restrict_to_admins_never_raises(monkeypatch, tmp_path):
    from pingpair.core import update_apply

    monkeypatch.setattr(update_apply.sys, "platform", "win32")

    def boom(*a, **k):
        raise OSError("icacls missing")

    monkeypatch.setattr("subprocess.run", boom)
    update_apply._restrict_to_admins(tmp_path)  # swallows the error
