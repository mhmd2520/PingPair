"""About tab: version, author, license, dependency credits, log access."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTextEdit,
    QVBoxLayout,
)

from .. import __version__, settings
from ..context import Role
from ..core import update_apply, updater
from ..core.updater import (
    GITHUB_REPO_URL,
    UpdateCheckResult,
    UpdateStatus,
    check_for_update,
)
from ..paths import PROJECT_ROOT, log_dir, user_data_dir
from ._base import BaseView
from ._sounds import SoundEvent, notify_sound

# fping / iperf3 upstream release pages for the bundled versions.
_FPING_URL = "https://github.com/schweikert/fping/releases/tag/v5.5"
_IPERF3_URL = "https://github.com/esnet/iperf/releases/tag/3.21"
_SITE_URL = "https://www.mhmd2520.com"
# Single source of truth for the repo link: derived from the updater's
# release coordinates so flipping RELEASE_REPO (playground -> production)
# repoints the About link and the updater together.
_GITHUB_URL = GITHUB_REPO_URL
LICENSES_FILENAME = "THIRD_PARTY_LICENSES.md"

# Status-line text colours, matching the project's semantic palette
# (PASS / WARN / FAIL greens-ambers-reds + the muted info grey). The About
# update line uses coloured *text* rather than a filled pill so it can host a
# clickable release hyperlink without an unreadable link-on-fill combo.
_STATUS_COLOURS = {
    "ok": "#1f9d55",
    "warn": "#c08400",
    "error": "#d05050",
    "info": "#8a97ad",
}


def _fmt_eta(seconds: float) -> str:
    """Format a download ETA: ``Xm Ys`` for >= 60 s, else ``Ns``.

    Keeps short ETAs compact ("~45s left") while making long ones readable
    ("~15m 1s left" instead of "~901s left").
    """
    secs = max(0, int(round(seconds)))
    if secs < 60:
        return f"{secs}s"
    minutes, rem = divmod(secs, 60)
    return f"{minutes}m {rem}s"


def _link_label(html: str) -> QLabel:
    """A QLabel whose ``<a href>`` links open in the system browser."""
    lbl = QLabel(html)
    lbl.setOpenExternalLinks(True)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    return lbl


_DEP_LIST = (
    "PySide6 (Qt 6, LGPL)",
    "pyqtgraph",
    "python-docx",
    "openpyxl",
    "reportlab",
    "pydantic",
    "psutil",
    "tabulate",
)


class _UpdateCheckWorker(QThread):
    """Runs the GitHub release check off the UI thread.

    ``check_for_update`` is total — it maps every failure onto a result —
    so ``run`` never raises and the handler always gets a result object.
    """

    done = Signal(object)  # UpdateCheckResult

    def __init__(self, current_version: str) -> None:
        super().__init__()
        self._current = current_version

    def run(self) -> None:  # noqa: D401 - QThread.run override
        self.done.emit(check_for_update(self._current))


@dataclass(frozen=True)
class _DownloadOutcome:
    """Result of a download+verify pass handed back to the GUI thread."""

    ok: bool
    path: Path | None = None
    error: str = ""
    cancelled: bool = False
    # Round-6 #4: set when the pre-flight reachability probe failed before any
    # download started, so the handler shows a friendly "no connection" message
    # (with Wi-Fi / Loopback hints) instead of a scary "download failed".
    preflight_failed: bool = False


def _is_https(url: str) -> bool:
    """True only for an ``https://`` URL (case-insensitive scheme)."""
    return url.lower().startswith("https://")


class _UpdateDownloadWorker(QThread):
    """Downloads + integrity-verifies the release bundle off the UI thread.

    Emits :attr:`progress` (bytes-so-far, total-or-0) as it streams, and
    :attr:`done` with a :class:`_DownloadOutcome` once finished. Like the
    check worker, ``run`` never raises — every failure maps onto an outcome.
    :meth:`cancel` aborts the in-flight download (polled each chunk) so the
    Cancel button and a mid-download window close return promptly.
    """

    progress = Signal(int, int)
    done = Signal(object)  # _DownloadOutcome

    def __init__(self, result: UpdateCheckResult, dest_dir: Path) -> None:
        super().__init__()
        self._result = result
        self._dest_dir = dest_dir
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D401 - QThread.run override
        asset = self._result.asset
        if asset is None:
            self.done.emit(_DownloadOutcome(False, error="No downloadable bundle."))
            return
        # Round-6 #4: a quick reachability probe before the (long) download, so
        # an offline machine fails fast and clearly rather than waiting out the
        # full download timeout. Skip it if the user already cancelled.
        if self._cancelled:
            self.done.emit(_DownloadOutcome(False, cancelled=True))
            return
        pre = updater.preflight_check()
        if not pre.ok:
            self.done.emit(
                _DownloadOutcome(False, error=pre.detail, preflight_failed=True)
            )
            return
        dest = self._dest_dir / asset.name
        # SEC (Feature-6 close-out): the bundle ships UNSIGNED, so the SHA-256
        # sidecar is the ONLY integrity gate. Refuse to download/install unless
        # (a) a checksum is present — a release without its .sha256 is a hard
        # error, never a silent skip — and (b) both URLs are HTTPS, so a
        # downgraded/spoofed http:// asset can't be fetched and then run elevated.
        if not self._result.sha256_url:
            self.done.emit(
                _DownloadOutcome(
                    False,
                    error="This release is missing its integrity checksum "
                    "(.sha256), so the download can't be verified. Update aborted.",
                )
            )
            return
        if not _is_https(asset.url) or not _is_https(self._result.sha256_url):
            self.done.emit(
                _DownloadOutcome(
                    False,
                    error="The update isn't served over a secure (HTTPS) "
                    "connection, so it was not downloaded.",
                )
            )
            return
        try:
            updater.download_file(
                asset.url,
                dest,
                progress=lambda g, t: self.progress.emit(g, t),
                cancelled=lambda: self._cancelled,
            )
            if self._cancelled:
                self.done.emit(_DownloadOutcome(False, cancelled=True))
                return
            expected = updater.parse_sha256_text(
                updater.fetch_text(self._result.sha256_url)
            )
            if not expected:
                self.done.emit(
                    _DownloadOutcome(
                        False,
                        error="Couldn't read the release's integrity checksum, "
                        "so the download can't be verified. Update aborted.",
                    )
                )
                return
            if not updater.verify_sha256(dest, expected):
                self.done.emit(
                    _DownloadOutcome(
                        False,
                        error="Downloaded file failed its integrity check.",
                    )
                )
                return
            self.done.emit(_DownloadOutcome(True, path=dest))
        except updater.DownloadCancelled:
            self.done.emit(_DownloadOutcome(False, cancelled=True))
        except updater.UpdateCheckError as exc:
            self.done.emit(_DownloadOutcome(False, error=str(exc)))
        except Exception as exc:  # noqa: BLE001 - worker must never raise
            self.done.emit(
                _DownloadOutcome(False, error=f"Unexpected error: {exc}")
            )


class _UpdateAvailableDialog(QDialog):
    """Modal 'update available' offer with a WORKING close (X) button.

    Replaces the old QMessageBox: a QMessageBox disables its title-bar X when
    there's no RejectRole/escape button, and we deliberately dropped the
    "Later" button (#2). A plain QDialog closes on X / Esc by default
    (``reject()``) — exactly the "dismiss, do nothing" behaviour we want.
    The user's choice is exposed as :attr:`choice`.
    """

    DISMISS = 0
    INSTALL = 1
    DONT_REMIND = 2

    def __init__(
        self,
        parent,
        *,
        latest: str,
        current: str,
        can_install: bool,
        release_notes: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update available")
        self.choice = self.DISMISS

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        # Header: info icon + message (a little above the 10 pt app default).
        head = QHBoxLayout()
        head.setSpacing(14)
        icon = QLabel()
        std = self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxInformation
        )
        icon.setPixmap(std.pixmap(48, 48))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(icon)
        body = (
            "Download and install it now?"
            if can_install
            else "Use the 'Update available' link in the About tab to download it."
        )
        msg = QLabel(
            f"<div style='font-size:11pt;'>"
            f"<b>PingPair {latest} is available.</b><br>"
            f"You have {current}.<br><br>{body}</div>"
        )
        msg.setWordWrap(True)
        head.addWidget(msg, 1)
        outer.addLayout(head)

        # Collapsible release notes ("What's new") — built hidden here, revealed
        # by the "Show details" button in the single action row below, and shown
        # beneath that row when toggled on.
        self._notes = None
        if release_notes:
            self._notes = QTextEdit()
            self._notes.setReadOnly(True)
            self._notes.setPlainText(release_notes)
            self._notes.setVisible(False)
            self._notes.setFixedHeight(160)

        # Action buttons — all on ONE row, left→right (#6):
        #   [Download && install]   [Show details]   [Don't remind me again]
        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch(1)
        if can_install:
            install = QPushButton("Download && install")
            install.setDefault(True)
            install.clicked.connect(self._on_install)
            btns.addWidget(install)
        if self._notes is not None:
            self._notes_btn = QPushButton("Show details")
            self._notes_btn.setCheckable(True)
            self._notes_btn.toggled.connect(self._toggle_notes)
            btns.addWidget(self._notes_btn)
        dont = QPushButton("Don't remind me again")
        dont.clicked.connect(self._on_dont_remind)
        btns.addWidget(dont)
        btns.addStretch(1)
        outer.addLayout(btns)
        if self._notes is not None:
            outer.addWidget(self._notes)

    @Slot(bool)
    def _toggle_notes(self, on: bool) -> None:
        self._notes.setVisible(on)
        self._notes_btn.setText("Hide details" if on else "Show details")
        self.adjustSize()

    def _on_install(self) -> None:
        self.choice = self.INSTALL
        self.accept()

    def _on_dont_remind(self) -> None:
        self.choice = self.DONT_REMIND
        self.accept()


class AboutView(BaseView):
    title = "About PingPair"

    def _build_placeholder(self) -> None:
        # One reusable worker reference; ``_manual_check`` records whether the
        # in-flight check came from the button (always shows a dialog) or the
        # auto-on-launch path (only pops a modal for a genuinely new version).
        self._worker: _UpdateCheckWorker | None = None
        self._manual_check = False
        # Download worker + the last installable result, so the card's
        # "Download & install" button can act without re-checking.
        self._dl_worker: _UpdateDownloadWorker | None = None
        self._pending_update: UpdateCheckResult | None = None
        self._dl_start_ts = 0.0  # set when a download starts (speed/ETA)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Product wordmark + one-line intro span the full width above the cards.
        outer.addWidget(QLabel(f"<h1>PingPair {__version__}</h1>"))
        intro = QLabel(
            "Automated LAN characterization between two Windows laptops "
            "using fping 5.5 and iperf3 3.21."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)
        # TEMP Feature-6 self-update test marker — only the v0.2.0 playground
        # build shows it, so the same source builds both the 0.1.0 "installed"
        # exe (no banner) and the 0.2.0 "update" exe (banner) by flipping only
        # __version__. Remove this whole block when reverting the test build.
        if __version__ == "0.2.0":
            test_banner = QLabel(
                "<b style='color:#1f9d55;'>You are running the SELF-UPDATED "
                "v0.2.0 build — the in-app updater worked!</b>"
            )
            test_banner.setWordWrap(True)
            outer.addWidget(test_banner)
        outer.addSpacing(12)

        # 2x2 card grid: About | Updates  (top) / Credits & License |
        # Diagnostics (bottom). Groups related info so the tab reads as four
        # tidy sections instead of one long column.
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.addWidget(self._build_about_card(), 0, 0)
        grid.addWidget(self._build_updates_card(), 0, 1)
        grid.addWidget(self._build_credits_card(), 1, 0)
        grid.addWidget(self._build_diagnostics_card(), 1, 1)
        outer.addLayout(grid)
        outer.addStretch(1)

    # ----- card builders ----------------------------------------------

    @staticmethod
    def _card(title: str) -> tuple[QGroupBox, QVBoxLayout]:
        """A titled group-box card with a top-aligned vertical layout."""
        box = QGroupBox(title)
        inner = QVBoxLayout(box)
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(8)
        inner.setAlignment(Qt.AlignmentFlag.AlignTop)
        return box, inner

    def _build_about_card(self) -> QGroupBox:
        box, inner = self._card("About")
        inner.addWidget(QLabel("<b>Made by:</b> Mohamed Khaled"))
        inner.addWidget(_link_label(
            f'<b>Website:</b> <a href="{_SITE_URL}">www.mhmd2520.com</a>'
        ))
        inner.addWidget(_link_label(
            f'<b>GitHub:</b> <a href="{_GITHUB_URL}">github.com/mhmd2520/PingPair</a>'
        ))
        inner.addWidget(_link_label(
            f'<b>Bundled-tool sources:</b> <a href="{_FPING_URL}">fping 5.5</a>'
            f' &nbsp;·&nbsp; <a href="{_IPERF3_URL}">iperf3 3.21</a>'
        ))
        return box

    def _build_updates_card(self) -> QGroupBox:
        box, inner = self._card("Updates")
        row = QHBoxLayout()
        self._check_btn = QPushButton("Check for updates")
        self._check_btn.clicked.connect(self._on_check_updates_clicked)
        row.addWidget(self._check_btn)
        # Appears only after a check finds an installable bundle on a packaged
        # build; hidden on a source checkout (nothing to swap).
        self._install_btn = QPushButton("Download && install")
        self._install_btn.clicked.connect(self._on_install_clicked)
        self._install_btn.setVisible(False)
        row.addWidget(self._install_btn)
        # Shown only while a download is in flight (see _confirm_and_download).
        self._cancel_btn = QPushButton("Cancel download")
        self._cancel_btn.clicked.connect(self._on_cancel_download)
        self._cancel_btn.setVisible(False)
        row.addWidget(self._cancel_btn)
        row.addStretch(1)
        inner.addLayout(row)

        self._update_status = _link_label("")
        self._update_status.setWordWrap(True)
        self._update_status.setVisible(False)
        inner.addWidget(self._update_status)

        self._download_bar = QProgressBar()
        self._download_bar.setVisible(False)
        inner.addWidget(self._download_bar)
        # Live "12.3 / 103.7 MB  ·  4.1 MB/s  ·  ~22s left" line under the bar.
        self._download_detail = QLabel("")
        self._download_detail.setStyleSheet(f"color:{_STATUS_COLOURS['info']};")
        self._download_detail.setVisible(False)
        inner.addWidget(self._download_detail)

        self._auto_check = QCheckBox("Check automatically on launch")
        self._auto_check.setChecked(settings.load_updates_auto_check())
        self._auto_check.toggled.connect(self._on_auto_check_toggled)
        inner.addWidget(self._auto_check)
        return box

    def _build_credits_card(self) -> QGroupBox:
        box, inner = self._card("Credits & License")
        built = QLabel("<b>Built with:</b> " + ", ".join(_DEP_LIST) + ".")
        built.setWordWrap(True)
        inner.addWidget(built)
        lic = QLabel(
            "<b>License:</b> Proprietary — © 2026 Mohamed Khaled. "
            "See the LICENSE file in the repository root."
        )
        lic.setWordWrap(True)
        inner.addWidget(lic)

        row = QHBoxLayout()
        lic_btn = QPushButton("Third-party licenses")
        lic_btn.clicked.connect(self._on_open_licenses)
        row.addWidget(lic_btn)
        row.addStretch(1)
        inner.addLayout(row)
        return box

    def _build_diagnostics_card(self) -> QGroupBox:
        box, inner = self._card("Diagnostics")
        paths = QLabel(
            f"User data: <code>{user_data_dir()}</code><br>"
            f"Log file:  <code>{log_dir() / 'pingpair.log'}</code>"
        )
        paths.setWordWrap(True)
        paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        inner.addWidget(paths)

        row = QHBoxLayout()
        for label, handler in (
            ("Open log folder", self._on_open_log_folder),
            ("Open user-data folder", self._on_open_user_data),
            ("Copy diagnostic info", self._on_copy_diagnostics),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            row.addWidget(btn)
        row.addStretch(1)
        inner.addLayout(row)
        return box

    # ----- handlers ---------------------------------------------------

    @Slot()
    def _on_open_log_folder(self) -> None:
        path = log_dir()
        path.mkdir(parents=True, exist_ok=True)
        _open_in_file_browser(path)

    @Slot()
    def _on_open_user_data(self) -> None:
        path = user_data_dir()
        path.mkdir(parents=True, exist_ok=True)
        _open_in_file_browser(path)

    @Slot()
    def _on_open_licenses(self) -> None:
        """Open the third-party license notices in the OS default app.

        The file ships at the ``Software/`` root (``PROJECT_ROOT``), which
        resolves in both dev and frozen one-folder builds. If it's missing
        (e.g. an incomplete checkout) we say so rather than failing silently.
        """
        path = PROJECT_ROOT / LICENSES_FILENAME
        if path.is_file():
            _open_in_file_browser(path)
        else:
            QMessageBox.information(
                self,
                "Third-party licenses",
                f"License notices file not found at:\n{path}",
            )

    @Slot()
    def _on_copy_diagnostics(self) -> None:
        """Snapshot system + app state to clipboard for support tickets."""
        from PySide6.QtWidgets import QApplication
        rs = self.ctx.run_state
        info = (
            f"PingPair {__version__}\n"
            f"Python {sys.version.split()[0]} on {sys.platform}\n"
            f"Role: {rs.role.value}\n"
            f"Server host override: {rs.server_host_override or '(none)'}\n"
            f"Loopback: {rs.loopback}\n"
            f"Report dir: {rs.report_dir}\n"
            f"Filename pattern: {rs.report_filename_pattern}\n"
            f"Formats: {rs.report_formats}\n"
            f"Auto-save: {rs.report_auto_save}\n"
            f"User data dir: {user_data_dir()}\n"
            f"Log file: {log_dir() / 'pingpair.log'}\n"
        )
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(info)

    # ----- update check (Feature 6) -----------------------------------

    @Slot()
    def _on_check_updates_clicked(self) -> None:
        self._start_check(manual=True)

    @Slot(bool)
    def _on_auto_check_toggled(self, checked: bool) -> None:
        settings.save_updates_auto_check(checked)

    def maybe_auto_check(self) -> None:
        """Fire the launch-time auto-check (best-effort).

        Runs on *every* launch so the user is reminded of a waiting update
        each time (the update modal carries a "Don't remind me again" opt-out
        — see :meth:`_show_update_available_dialog`). Skipped only when the
        user has opted out. Fires in **every** role including Loopback: a dev
        box usually has internet at launch, the manual check already worked in
        Loopback, and the launch check is wanted for update-flow testing. A
        genuinely offline machine surfaces a friendly role-aware error rather
        than silently doing nothing. The timestamp is recorded for diagnostics.
        """
        if not settings.load_updates_auto_check():
            return
        settings.save_updates_last_check_ts(time.time())
        self._start_check(manual=False)

    def _start_check(self, *, manual: bool) -> None:
        """Spawn the release-check worker, unless one is already running."""
        if self._worker is not None:
            try:
                if self._worker.isRunning():
                    return
            except RuntimeError:
                self._worker = None
        self._manual_check = manual
        self._check_btn.setEnabled(False)
        self._set_update_status("Checking for updates...", "info")

        worker = _UpdateCheckWorker(__version__)
        # QueuedConnection so the result slot runs on the GUI thread.
        worker.done.connect(self._on_check_done, Qt.ConnectionType.QueuedConnection)
        # Round-21 (YY): drop our reference only on the *built-in* finished
        # signal (after the thread truly exits), never on the custom ``done``
        # signal which fires while run() is still on the stack — otherwise a
        # close mid-check makes shutdown() skip its wait() and Qt aborts.
        worker.finished.connect(self._on_worker_thread_finished)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    @Slot(object)
    def _on_check_done(self, result: UpdateCheckResult) -> None:
        # The result handler owns re-enabling the button (mirrors PingView's
        # _on_finished); the built-in finished slot only drops the worker ref.
        self._check_btn.setEnabled(True)
        self._set_update_status_from_result(result)

        # Reveal the inline install button when there's a bundle we can
        # actually apply on this (packaged) build, so the user can install
        # later without re-checking. Hidden while a download is in flight.
        installable = result.can_self_install and update_apply.is_frozen()
        self._pending_update = result if installable else None
        self._install_btn.setVisible(installable and self._dl_worker is None)

        # Manual checks always show a dialog. Auto checks show the
        # update-available modal on EVERY launch when an update exists (the
        # user opts out via the modal's "Don't remind me again").
        if self._manual_check:
            self._show_manual_result(result)
        elif result.update_available:
            self._show_update_available_dialog(result)

    @Slot()
    def _on_worker_thread_finished(self) -> None:
        # Built-in finished — the thread has truly exited, so it's safe to drop
        # the reference. Sender guard ignores a stale finished from an
        # already-replaced worker. (Round-21 YY; mirrors PingView.)
        if self.sender() is self._worker:
            self._worker = None

    def _set_update_status(self, text: str, level: str) -> None:
        colour = _STATUS_COLOURS.get(level, _STATUS_COLOURS["info"])
        self._update_status.setText(text)
        self._update_status.setStyleSheet(f"color:{colour};")
        self._update_status.setVisible(True)

    def _set_update_status_from_result(self, result: UpdateCheckResult) -> None:
        if result.update_available:
            self._set_update_status(
                f'Update available: '
                f'<a href="{result.release_url}">v{result.latest_version}</a>',
                "warn",
            )
        elif result.status is UpdateStatus.UP_TO_DATE:
            self._set_update_status(
                f"You're up to date (v{result.current_version}).", "ok"
            )
        elif result.status is UpdateStatus.NO_RELEASE:
            self._set_update_status("No public release published yet.", "info")
        else:  # ERROR
            self._set_update_status("Couldn't reach GitHub.", "error")

    def _show_manual_result(self, result: UpdateCheckResult) -> None:
        """Always show a dialog for a button-initiated check."""
        if result.update_available:
            self._show_update_available_dialog(result)
            return
        if result.status is UpdateStatus.ERROR:
            # Round-7 #3: a reachability failure gets the friendly, structured
            # "no internet" dialog (cause + fix, raw error behind Show Details).
            self._show_connection_error(result.detail)
            return
        notify_sound(SoundEvent.SUCCESS)  # Round-7 #2: a gentle info chime
        box = QMessageBox(self)
        box.setWindowTitle("Check for updates")
        if result.status is UpdateStatus.UP_TO_DATE:
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(
                f"<b>You're up to date.</b><br>"
                f"PingPair {result.current_version} is the latest release."
            )
        else:  # NO_RELEASE
            box.setIcon(QMessageBox.Icon.Information)
            box.setText("<b>No updates yet.</b>")
            box.setInformativeText(
                result.detail or "No public release has been published."
            )
        box.exec()

    def _show_update_available_dialog(self, result: UpdateCheckResult) -> None:
        """Modal offer to install the update in-app (packaged build).

        Custom dialog (not QMessageBox) so the title-bar X / Esc reliably
        dismiss without any action — QMessageBox disables its X when no
        RejectRole button is present, and we dropped "Later" (#2). Actions:
        Download && install, Show details (release notes), Don't remind me
        again. The release page stays reachable via the inline "Update
        available: vX" link in the card.
        """
        can_install = result.can_self_install and update_apply.is_frozen()
        notify_sound(SoundEvent.PROMPT)  # Round-7 #2: alert that an update awaits
        dlg = _UpdateAvailableDialog(
            self,
            latest=result.latest_version,
            current=result.current_version,
            can_install=can_install,
            release_notes=result.release_notes,
        )
        dlg.exec()
        if dlg.choice == _UpdateAvailableDialog.INSTALL:
            self._confirm_and_download(result)
        elif dlg.choice == _UpdateAvailableDialog.DONT_REMIND:
            settings.save_updates_auto_check(False)
            self._auto_check.setChecked(False)
        # DISMISS (X / Esc) → do nothing, like the old "Later".

    # ----- download + install -----------------------------------------

    @Slot()
    def _on_install_clicked(self) -> None:
        if self._pending_update is not None:
            self._confirm_and_download(self._pending_update)

    def _confirm_and_download(self, result: UpdateCheckResult) -> None:
        """Confirm, then start the background download of the bundle."""
        if self._dl_worker is not None:
            try:
                if self._dl_worker.isRunning():
                    return
            except RuntimeError:
                self._dl_worker = None
        # Constraint: never swap the install out from under a running sweep.
        if self.ctx.run_state.sweep_active:
            QMessageBox.warning(
                self,
                "Update blocked",
                "A test sweep is running. Stop the sweep before updating — "
                "PingPair must close to install the new version.",
            )
            return
        asset = result.asset
        if asset is None:
            return
        size_mb = asset.size / (1024 * 1024) if asset.size else 0
        size_txt = f" (~{size_mb:.0f} MB)" if size_mb else ""
        confirm = QMessageBox.question(
            self,
            "Download && install update",
            f"Download PingPair {result.latest_version}{size_txt} and install "
            "it now?\n\nPingPair will close and reopen on the new version.",
        )
        if confirm is not QMessageBox.StandardButton.Yes:
            return

        self._install_btn.setVisible(False)
        self._check_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._cancel_btn.setEnabled(True)
        self._download_bar.setVisible(True)
        self._download_bar.setRange(0, 0)  # indeterminate until first progress
        self._download_detail.setVisible(True)
        # The worker pre-flights the connection first (Round-6 #4), so reflect
        # that as the opening state; the live speed/ETA line takes over once
        # bytes start arriving.
        self._download_detail.setText("Checking connection...")
        self._dl_start_ts = time.time()
        self._set_update_status(f"Updating to v{result.latest_version}...", "info")

        # Auto-clean: clear any stale partial download / staging from a prior
        # attempt before starting fresh, so the cache never accumulates.
        update_apply.clear_update_cache()
        worker = _UpdateDownloadWorker(
            result, update_apply.update_cache_dir() / "download"
        )
        worker.progress.connect(
            self._on_download_progress, Qt.ConnectionType.QueuedConnection
        )
        worker.done.connect(
            self._on_download_done, Qt.ConnectionType.QueuedConnection
        )
        worker.finished.connect(self._on_dl_thread_finished)
        worker.finished.connect(worker.deleteLater)
        self._dl_worker = worker
        worker.start()

    @Slot()
    def _on_cancel_download(self) -> None:
        worker = self._dl_worker
        if worker is not None:
            self._cancel_btn.setEnabled(False)
            self._download_detail.setText("Cancelling...")
            try:
                worker.cancel()
            except RuntimeError:
                pass

    @Slot(int, int)
    def _on_download_progress(self, got: int, total: int) -> None:
        if total > 0:
            self._download_bar.setRange(0, total)
            self._download_bar.setValue(got)
        else:
            self._download_bar.setRange(0, 0)  # keep the busy indicator
        self._download_detail.setText(self._format_progress(got, total))

    def _format_progress(self, got: int, total: int) -> str:
        """'12.3 / 103.7 MB · 4.1 MB/s · ~22s left' from bytes + elapsed."""
        mb = 1024 * 1024
        elapsed = max(1e-6, time.time() - self._dl_start_ts)
        speed = got / elapsed  # bytes/s
        got_txt = f"{got / mb:.1f}"
        size_txt = f"{total / mb:.1f} MB" if total > 0 else "?"
        parts = [f"{got_txt} / {size_txt}"]
        if speed > 0:
            parts.append(f"{speed / mb:.1f} MB/s")
            if total > got:
                eta = (total - got) / speed
                parts.append(f"~{_fmt_eta(eta)} left")
        return "  ·  ".join(parts)

    @Slot(object)
    def _on_download_done(self, outcome: _DownloadOutcome) -> None:
        self._download_bar.setVisible(False)
        self._download_detail.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._check_btn.setEnabled(True)
        self._install_btn.setEnabled(True)
        if outcome.cancelled:
            self._set_update_status("Download cancelled.", "info")
            # Auto-clean the partial download so a cancel leaves nothing behind.
            update_apply.clear_update_cache()
            # Re-offer install if an update is still pending.
            self._install_btn.setVisible(self._pending_update is not None)
            return
        if outcome.preflight_failed:
            # Round-6 #4 / Round-7 #3: no internet route — show the friendly,
            # state-aware "no connection" dialog (cause + fix, raw error behind
            # Show Details) instead of a scary "download failed", since nothing
            # was ever downloaded.
            self._set_update_status("No internet connection.", "warn")
            update_apply.clear_update_cache()
            self._install_btn.setVisible(self._pending_update is not None)
            self._show_connection_error(outcome.error)
            return
        if not outcome.ok or outcome.path is None:
            self._set_update_status("Download failed.", "error")
            update_apply.clear_update_cache()
            self._install_btn.setVisible(self._pending_update is not None)
            notify_sound(SoundEvent.ERROR)  # Round-7 #2
            QMessageBox.warning(
                self,
                "Update download failed",
                outcome.error or "The update could not be downloaded.",
            )
            return
        self._apply_and_restart(outcome.path)

    @Slot()
    def _on_dl_thread_finished(self) -> None:
        if self.sender() is self._dl_worker:
            self._dl_worker = None

    def _apply_and_restart(self, bundle_path: Path) -> None:
        """Stage the downloaded bundle, launch the swap helper, and quit."""
        try:
            update_apply.apply_update(bundle_path)
        except update_apply.SourceInstallError as exc:
            QMessageBox.information(self, "Running from source", str(exc))
            return
        except update_apply.UpdateApplyError as exc:
            self._set_update_status("Install failed.", "error")
            QMessageBox.warning(self, "Update install failed", str(exc))
            return
        # The detached helper waits for this process to exit before swapping,
        # so quit now — it relaunches the new build when the locks release.
        QMessageBox.information(
            self,
            "Updating PingPair",
            "PingPair will now close and reopen on the new version.",
        )
        QApplication.quit()

    def _show_connection_error(self, raw_detail: str) -> None:
        """Friendly, structured 'no internet' dialog (Round-7 #3).

        Leads with a plain-language cause + a concrete fix, tucks the raw
        technical reason (e.g. ``[Errno 11001] getaddrinfo failed``) behind the
        QMessageBox "Show Details…" disclosure so it's available for support
        without confusing the user, and plays the error sound (Round-7 #2)."""
        notify_sound(SoundEvent.ERROR)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("No internet connection")
        box.setText(
            "<b>PingPair couldn't reach the internet to check for updates.</b>"
        )
        box.setInformativeText(self._connection_help())
        if raw_detail:
            box.setDetailedText(f"Technical detail:\n{raw_detail}")
        box.exec()

    def _connection_help(self) -> str:
        """Plain-language cause + fix for a reachability failure, role-aware."""
        rs = self.ctx.run_state
        if rs.role is Role.LOOPBACK:
            return (
                "You're in Loopback dev mode, which runs offline — so this is "
                "expected. Switch to the Server or Client role on a PC with "
                "internet access to check for updates."
            )
        lines = [
            "This almost always means this PC isn't connected to the internet "
            "right now.",
            "",
            "To update: connect to a network that has internet access (Wi-Fi, "
            'or an Ethernet port that reaches the internet), then click '
            '"Check for updates" again.',
        ]
        if rs.wifi_offline_adapter:
            lines += [
                "",
                "Note: Wi-Fi is currently turned off for testing. Re-enable it "
                "(or just close PingPair, which restores it automatically), "
                "then try again.",
            ]
        elif rs.role in (Role.SERVER, Role.CLIENT):
            lines += [
                "",
                "Tip: during a PingPair test this PC uses a direct Ethernet "
                "link with no internet, so updates won't work until you "
                "reconnect to your normal network.",
            ]
        return "\n".join(lines)

    def shutdown(self) -> None:
        """Stop in-flight check / download workers before teardown.

        Mirrors SetupView.shutdown — closing mid-work would otherwise
        destroy a running QThread and trigger Qt's "QThread: Destroyed
        while thread is still running" abort. The download worker is
        *cancelled first* (it polls the flag each chunk) so closing the
        window mid-download returns quickly instead of blocking on the
        full 60 s download timeout. One caveat (Round-6 #4): the cancel
        flag isn't polled during the pre-flight probe, so a close *during*
        pre-flight blocks up to its ~5 s timeout — still well under the
        8 s ``wait()`` ceiling. Called by MainWindow.closeEvent.
        """
        # Cancel the download up front so its wait() returns promptly.
        dl = self._dl_worker
        if dl is not None:
            try:
                dl.cancel()
            except RuntimeError:
                pass
        for attr, ceiling in (("_worker", 12000), ("_dl_worker", 8000)):
            worker = getattr(self, attr, None)
            setattr(self, attr, None)
            if worker is None:
                continue
            try:
                if worker.isRunning():
                    worker.wait(ceiling)
            except RuntimeError:
                pass  # libshiboken: underlying C++ object already deleted


def _open_in_file_browser(path: Path) -> None:
    """Best-effort open a folder (or file) in the OS default handler."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass
