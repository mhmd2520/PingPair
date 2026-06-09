"""Qt application bootstrap and main window.

Phase 4.6+: persistent settings via QSettings, app icon set from the
:mod:`branding` module, log file under ``%APPDATA%\\PingPair\\logs\\``
already wired in :mod:`context`.
"""

from __future__ import annotations

import logging
import os
import sys
from types import TracebackType

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, settings
from .branding import app_icon
from .context import AppContext, Role
from .core.role_detect import (
    detect_role,
    evaluate_role_ip_warning,
    local_ipv4_addresses,
)
from .views import (
    AboutView,
    AnalysisView,
    ConfigView,
    HelpView,
    PingView,
    ReportView,
    ScriptView,
    SetupView,
)


class MainWindow(QMainWindow):
    """Top-level window: title bar + tabbed pages + status bar."""

    def __init__(self, ctx: AppContext, *, loopback: bool = False) -> None:
        super().__init__()
        self.ctx = ctx
        self.loopback = loopback
        # Set True by the Setup tab "Reset all settings" action so
        # closeEvent skips the settings save (see closeEvent).
        self._suppress_close_save = False

        self.setWindowTitle(f"PingPair {__version__}")
        self.setWindowIcon(app_icon())

        # Group C-1 added enough new widgets (Continuous mode group,
        # extra Sweep subset toggles, wider Run-checkbox column) that
        # the old 1100x720 default + restored geometries from earlier
        # versions clip the right edge of the Live log pane. Set both
        # an initial size and a minimum so old persisted geometries
        # get clamped up.
        self.setMinimumSize(1200, 720)
        self.resize(1280, 800)

        # Restore window geometry from previous session if available.
        # Apply minimum AGAIN afterwards in case the persisted geometry
        # is smaller than the new minimum — restoreGeometry happily
        # honours whatever was saved, even if it's now too small.
        saved = settings.load_window_geometry()
        if saved is not None:
            if not self.restoreGeometry(saved):
                self.ctx.logger.warning(
                    "saved window geometry could not be restored "
                    "(corrupt blob or incompatible Qt version) — "
                    "using the default size"
                )
            if self.width() < 1200 or self.height() < 720:
                self.resize(
                    max(self.width(), 1280),
                    max(self.height(), 800),
                )
            self._ensure_on_screen()

        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._tabs.setMovable(False)

        # Order matters - Prereqs is first because it gates everything else.
        # Analysis sits between Report and Help so it reads as the natural
        # next step after producing a report: "saved a sweep? open
        # Analysis to compare it against an earlier one."
        self._tabs.addTab(SetupView(ctx), "Setup")
        self._tabs.addTab(PingView(ctx), "Ping")
        self._tabs.addTab(ConfigView(ctx), "Config")
        self._tabs.addTab(ScriptView(ctx), "Run")
        self._tabs.addTab(ReportView(ctx), "Save Options")
        self._tabs.addTab(AnalysisView(ctx), "Analysis")
        self._tabs.addTab(HelpView(ctx), "Help")
        # Kept as an attribute so the launch-time auto update-check can reach
        # it (see maybe_auto_update_check); the others stay anonymous.
        self._about_view = AboutView(ctx)
        self._tabs.addTab(self._about_view, "About")

        # Re-run the active tab's refresh() on activation so views can
        # re-read state that may have changed elsewhere (Setup re-runs
        # prereqs, Report re-scans the destination folder, etc.).
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Wire the Config tab's Apply button to rebuild the Run tab
        # (so the sweep grid reflects the imported payloads × bandwidths)
        # and refresh the Setup tab (so the prereq rows re-evaluate
        # against the new server / client IPs).  Plain callable — no
        # Qt signals needed; AppContext fires every listener
        # synchronously on the GUI thread.
        self.ctx.config_changed_listeners.append(self._on_config_changed)

        # Wire the cross-tab Help navigation hook so any view can route an
        # error to the relevant guide section (see :meth:`open_help`).
        self.ctx.open_help = self.open_help

        # Restore last active tab.
        last = settings.load_active_tab()
        if 0 <= last < self._tabs.count():
            self._tabs.setCurrentIndex(last)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)

        # Top role banner - colour-codes which side we're playing.
        # Kept as an instance attribute so :meth:`refresh_role_banner`
        # can re-render it after the user changes role on the Setup tab.
        self._role_banner = QLabel()
        self._role_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._role_banner)
        self.refresh_role_banner()

        # Persistent role/IP-mismatch warning banner with a Dismiss
        # button. Always visible on every tab (under the blue role
        # banner) when ctx.run_state.role_warning_text is non-empty
        # AND the user hasn't dismissed it for this session. The
        # Setup tab still shows the full warning + IP-fix action;
        # this is just the omnipresent reminder. (Task A, 2026-05-12.)
        from PySide6.QtWidgets import QPushButton as _QPush, QHBoxLayout as _QHB, QWidget as _QW
        self._warning_row = _QW()
        warn_layout = _QHB(self._warning_row)
        warn_layout.setContentsMargins(8, 6, 8, 6)
        self._warning_label = QLabel()
        self._warning_label.setStyleSheet(
            "background:#cc7a00; color:#fff; padding:6px; border-radius:4px;"
        )
        self._warning_label.setWordWrap(True)
        warn_layout.addWidget(self._warning_label, stretch=1)
        self._warning_dismiss = _QPush("Dismiss")
        self._warning_dismiss.setToolTip(
            "Hide this top banner for this session. The full warning + "
            "IP-fix action stays visible on the Setup tab. Reappears "
            "next launch if still applicable."
        )
        self._warning_dismiss.clicked.connect(self._on_dismiss_warning)
        _warn_btn_qss = (
            "QPushButton { background:#ffffff; color:#cc7a00; "
            "padding:4px 10px; border-radius:3px; border:none; }"
            "QPushButton:hover { background:#fff5e6; }"
        )
        # "How to fix" routes straight to the Troubleshooting guide section.
        self._warning_help = _QPush("How to fix")
        self._warning_help.setToolTip(
            "Open the Help tab's Troubleshooting section for step-by-step fixes."
        )
        self._warning_help.clicked.connect(lambda: self.open_help("troubleshooting"))
        self._warning_help.setStyleSheet(_warn_btn_qss)
        warn_layout.addWidget(self._warning_help)
        self._warning_dismiss.setStyleSheet(_warn_btn_qss)
        warn_layout.addWidget(self._warning_dismiss)
        self._warning_dismissed: bool = False
        # Last banner text actually composed — lets refresh_warning_banner
        # tell "the same warning is still showing" from "a NEW warning
        # arrived" so a prior Dismiss doesn't suppress the new one.
        self._last_warning_text: str = ""
        self._warning_row.setVisible(False)
        layout.addWidget(self._warning_row)
        self.refresh_warning_banner()

        layout.addWidget(self._tabs)
        self.setCentralWidget(container)

        status = QStatusBar()
        status.showMessage("Ready")
        self.setStatusBar(status)

    def _ensure_on_screen(self) -> None:
        """Recenter the window if the restored geometry is off-screen.

        A geometry saved on a monitor that has since been disconnected
        can restore the window fully outside every available screen,
        leaving the user no way to drag it back. If the restored frame
        intersects no screen's available area, recenter on the primary.
        """
        frame = self.geometry()
        for screen in QApplication.screens():
            if screen.availableGeometry().intersects(frame):
                return
        primary = QApplication.primaryScreen()
        if primary is not None:
            frame.moveCenter(primary.availableGeometry().center())
            self.move(frame.topLeft())

    # ----- role transitions --------------------------------------------

    def refresh_role_banner(self) -> None:
        """Re-render the top banner from the current ``ctx.run_state.role``.

        Called once during ``__init__`` and again whenever the Prereqs
        tab's role switcher applies a new role.
        """
        ctx = self.ctx
        role = ctx.run_state.role
        if role is Role.SERVER:
            banner_text = (
                f"Server role - listening on {ctx.config.network.server_ip}"
                f":{ctx.config.network.control_port} (control) and 5201 (iperf3)"
            )
            banner_bg = "#1f6f3a"
        elif role is Role.CLIENT:
            host = ctx.run_state.server_host_override or str(ctx.config.network.server_ip)
            banner_text = f"Client role - drives sweeps against Server at {host}"
            banner_bg = "#1565c0"
        elif role is Role.LOOPBACK or self.loopback:
            banner_text = "Loopback dev mode - both roles on 127.0.0.1"
            banner_bg = "#a06000"
        else:
            banner_text = "Role: undecided - open Setup to choose"
            banner_bg = "#555555"

        self._role_banner.setText(banner_text)
        self._role_banner.setStyleSheet(
            f"background:{banner_bg}; color:#fff; padding:6px; border-radius:4px;"
        )

    def refresh_warning_banner(self) -> None:
        """Re-render the persistent role/IP / connection warning banner.

        Reads ``ctx.run_state.role_warning_text`` AND
        ``ctx.run_state.connection_warning_text``. When either is
        non-empty AND the user hasn't pressed Dismiss this session,
        the banner shows as an orange row under the blue role banner.
        If both are set we concatenate them with a separator so the
        user sees the full picture. The Dismiss flag does not survive
        an app restart. (Task T, 2026-05-13: extended for connection
        errors.)
        """
        parts: list[str] = []
        role_text = self.ctx.run_state.role_warning_text or ""
        if role_text:
            parts.append(role_text)
        conn_text = getattr(
            self.ctx.run_state, "connection_warning_text", ""
        ) or ""
        if conn_text:
            parts.append(conn_text)
        text = "  ·  ".join(parts)
        # A *new* warning overrides a prior session-dismiss: the user
        # dismissed the old message, not this one. Without this reset a
        # single Dismiss would hide every later warning (e.g. a mid-sweep
        # "Server connection error") for the rest of the session. The same
        # text re-firing stays dismissed.
        if text and text != self._last_warning_text:
            self._warning_dismissed = False
        self._last_warning_text = text
        if text and not self._warning_dismissed:
            self._warning_label.setText(text)
            self._warning_row.setVisible(True)
        else:
            self._warning_row.setVisible(False)

    def open_help(self, key: str = "troubleshooting") -> None:
        """Switch to the Help tab and open the section matching ``key``.

        Wired onto :attr:`AppContext.open_help` so any view can turn an error
        popup or warning banner into a one-click route to the relevant guide
        section (``key`` is a section's cross-link key, e.g. ``"setup"`` or
        ``"troubleshooting"``). Safe no-op if the Help tab or section is gone.
        """
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == "Help":
                self._tabs.setCurrentIndex(i)
                opener = getattr(self._tabs.widget(i), "open_section", None)
                if callable(opener):
                    opener(key)
                return

    def _on_dismiss_warning(self) -> None:
        """Hide the top banner for this session only.

        The Setup tab keeps the full warning + fix-action button;
        this just suppresses the omnipresent reminder so the user
        can work in other tabs without it occupying screen real
        estate.
        """
        self._warning_dismissed = True
        self._warning_row.setVisible(False)

    def rebuild_script_tab(self) -> None:
        """Tear down and re-create the Run tab so it picks up the new role.

        :class:`ScriptView` chooses Loopback / Server / Client panel at
        construction time and never re-checks. After a Setup tab role
        change, this method swaps in a fresh ScriptView so the user
        doesn't have to restart the app.
        """
        # Find the Run tab (its title is fixed at construction).
        script_idx = -1
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == "Run":
                script_idx = i
                break
        if script_idx < 0:
            return

        # Remember whether the Run tab was active so we can restore.
        was_active = self._tabs.currentIndex() == script_idx

        old = self._tabs.widget(script_idx)
        self._tabs.removeTab(script_idx)
        if old is not None:
            # Stop the old panel's background worker (Server listener or
            # in-flight sweep) before dropping the view — the worker is
            # not a Qt child of the view, so deleteLater alone would
            # leave the thread running detached.
            if hasattr(old, "shutdown"):
                try:
                    old.shutdown()
                except Exception:  # noqa: BLE001
                    self.ctx.logger.exception("Run tab shutdown failed")
            old.deleteLater()

        new_view = ScriptView(self.ctx)
        self._tabs.insertTab(script_idx, new_view, "Run")
        if was_active:
            self._tabs.setCurrentIndex(script_idx)

        self.refresh_role_banner()

    def _on_config_changed(self) -> None:
        """Rebuild Run tab + refresh Setup tab after a Config tab Apply.

        Called from :meth:`AppContext.notify_config_changed`.  The
        Run tab's :class:`SweepTable` reads
        ``ctx.config.test_plan.payloads_bytes`` / ``bandwidths_mbps``
        only at construction time, so we rebuild the whole panel —
        matches the role-change rebuild flow.  Setup tab's
        :meth:`refresh` re-runs prereqs against the (possibly new)
        IPs.
        """
        try:
            self.rebuild_script_tab()
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception(
                "rebuild_script_tab failed after config change"
            )
        # Refresh Setup tab if it has a refresh() method (the SetupView
        # uses it for prereq re-runs).
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == "Setup":
                widget = self._tabs.widget(i)
                # A config change moves the IPs — force an unconditional
                # prereq re-run, bypassing the tab-activation debounce.
                refresh = getattr(widget, "force_refresh", None) or getattr(
                    widget, "refresh", None
                )
                if refresh is not None:
                    try:
                        refresh()
                    except Exception:  # noqa: BLE001
                        self.ctx.logger.exception(
                            "Setup tab refresh failed after config change"
                        )
                break

    # ----- lifecycle ---------------------------------------------------

    def _on_tab_changed(self, index: int) -> None:
        widget = self._tabs.widget(index)
        if hasattr(widget, "refresh"):
            widget.refresh()

    def _maybe_restore_wifi_on_close(self) -> None:
        """Re-enable Wi-Fi on close if the test disabled it.

        Windows then auto-reconnects to the saved network on its own (the
        profile is left on auto-connect, so there's nothing else to restore).
        Fired **detached** so closing the app stays instant — running netsh
        inline here was a source of "not responding" on close. Admin-gated;
        no-op when PingPair never touched Wi-Fi this session.
        """
        adapter = getattr(self.ctx.run_state, "wifi_offline_adapter", None)
        if not adapter:
            return
        from .core.fix_actions import is_admin
        if not is_admin():
            return
        from .core.state_capture import _build_enable_wifi_argv
        from .core.winexec import harden_argv
        import subprocess
        try:
            flags = (
                subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
            )
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                # Absolute System32 netsh — runs elevated (see winexec).
                harden_argv(_build_enable_wifi_argv(adapter)),
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.ctx.logger.info("re-enabling Wi-Fi on close (adapter=%s)", adapter)
            # Only clear the crash-recovery marker once the re-enable has
            # actually been launched. If the Popen above raises (netsh
            # missing, elevation lost, …) the marker MUST survive so the next
            # launch's _restore_wifi_if_pending retries — otherwise a failed
            # close-time restore would strand the user's Wi-Fi off for good.
            settings.save_wifi_offline_adapter(None)
            self.ctx.run_state.wifi_offline_adapter = None
        except Exception:  # noqa: BLE001 - never let close hang/raise on this
            self.ctx.logger.exception(
                "Wi-Fi restore on close failed — keeping the marker so the "
                "next launch retries"
            )

    def _maybe_restore_ethernet_on_close(self) -> None:
        """Revert the primary Ethernet NIC to DHCP on close (the X path).

        Reverts to DHCP with the **same command "Reset all settings" runs**
        for the Ethernet NIC (`build_release_dhcp_argv` via
        `run_restore_commands`), but ONLY when the primary Ethernet is bound to
        an IP **inside the test subnet** — i.e. it's plainly PingPair's test
        address (the user's rig: Client on ``192.168.1.2``). The whether-to-run
        decision is the pure :func:`core.state_capture.plan_close_ethernet_revert`.

        The subnet gate keeps a *silent* close from clobbering a config that
        isn't ours — an unrelated static on another subnet, or a Loopback-dev
        box PingPair never configured, is left untouched (CLAUDE.md §2: never
        modify the host without explicit confirmation; Reset stays unconditional
        because it's an explicit wipe). A light psutil-only probe (no
        `capture_snapshot` firewall dump) keeps close fast. Admin-gated; never
        raises. Wi-Fi is handled separately by
        :meth:`_maybe_restore_wifi_on_close`; firewall rules persist on close.
        """
        from .core.fix_actions import is_admin
        if not is_admin():
            self.ctx.logger.info("Ethernet close-restore: skipped (not admin)")
            return
        try:
            from .core.fix_actions import detect_primary_adapter
            from .core.state_capture import (
                _psutil_ipv4,
                plan_close_ethernet_revert,
                run_restore_commands,
            )

            adapter = detect_primary_adapter()
            current_ip, _ = _psutil_ipv4(adapter) if adapter else (None, None)
            commands = plan_close_ethernet_revert(
                adapter,
                current_ip,
                str(self.ctx.config.network.server_ip),
                self.ctx.config.network.subnet_mask,
            )
            if not commands:
                self.ctx.logger.info(
                    "Ethernet close-restore: adapter=%s ip=%s not on test "
                    "subnet -> leaving it alone",
                    adapter, current_ip,
                )
                return
            ok, transcript = run_restore_commands(commands)
            self.ctx.logger.info(
                "Ethernet close-restore: adapter=%s ip=%s -> reverted to "
                "DHCP ok=%s",
                adapter, current_ip, ok,
            )
            # If the revert failed the NIC is stranded on the test IP — record
            # the adapter so the next launch finishes the job (crash-recovery,
            # mirror of the Wi-Fi marker). Clear it on success.
            settings.save_ethernet_revert_pending(None if ok else adapter)
            if not ok:
                self.ctx.logger.warning(
                    "Ethernet close-restore transcript:\n%s", transcript
                )
        except Exception:  # noqa: BLE001 - never let close hang/raise on this
            self.ctx.logger.exception("Ethernet restore on close failed")

    def maybe_auto_update_check(self) -> None:
        """Delegate the launch-time auto update-check to the About tab.

        Fired from a post-show QTimer (see ``_run_welcome_then_show``).
        Best-effort: a failure here must never disrupt a started session.
        """
        try:
            self._about_view.maybe_auto_check()
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("auto update-check failed to start")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Persist user state and stop background threads before closing."""
        # The Setup tab "Reset all settings" sets this flag so the close
        # doesn't re-persist the in-memory RunState over the QSettings
        # store it just cleared.
        if not self._suppress_close_save:
            try:
                settings.save_from(self.ctx.run_state)
                settings.save_window_geometry(bytes(self.saveGeometry()))
                settings.save_active_tab(self._tabs.currentIndex())
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(
                    "could not save settings on close: %s", exc
                )
            # Give the user's Wi-Fi back if PingPair took it offline this
            # session — closing the app shouldn't leave them without internet.
            # (Reset already restores Wi-Fi on its own; its close is suppressed
            # so this won't double-run.)
            self._maybe_restore_wifi_on_close()
            # Revert the Ethernet NIC to its pre-PingPair config if the "Set
            # the correct IP" fix changed it (symmetric with Wi-Fi). Reset
            # reverts Ethernet independently and suppresses close, so this
            # won't double-run.
            self._maybe_restore_ethernet_on_close()
        # Stop any tab's background worker (the Server panel's listener
        # thread runs continuously, a sweep may be in flight) so no
        # QThread outlives the QApplication — that triggers Qt's
        # "Destroyed while thread is still running" warning / crash and
        # can orphan the iperf3/fping subprocesses.
        for i in range(self._tabs.count()):
            widget = self._tabs.widget(i)
            if hasattr(widget, "shutdown"):
                try:
                    widget.shutdown()
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning(
                        "tab shutdown failed on close: %s", exc
                    )
        super().closeEvent(event)


def _restore_wifi_if_pending(ctx: AppContext) -> None:
    """Re-enable a Wi-Fi adapter a prior session disabled but didn't restore.

    Crash-recovery: the "Disable Wi-Fi" fix persists the adapter name; a
    normal close clears it after re-enabling. If the marker survives to the
    next launch the previous session was force-killed / crashed, so re-enable
    that adapter now (Windows then auto-reconnects). Best-effort, admin-gated,
    fired detached so it never blocks startup.
    """
    pending = settings.load_wifi_offline_adapter()
    if not pending:
        return
    from .core.fix_actions import is_admin
    if not is_admin():
        return  # can't re-enable now; leave the marker for a later elevated run
    import subprocess

    from .core.state_capture import _build_enable_wifi_argv
    from .core.winexec import harden_argv
    try:
        flags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            # Absolute System32 netsh — runs elevated (see winexec).
            harden_argv(_build_enable_wifi_argv(pending)),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ctx.logger.info(
            "crash-recovery: re-enabling Wi-Fi left disabled by a prior "
            "session (adapter=%s)", pending,
        )
    except Exception:  # noqa: BLE001
        ctx.logger.exception("crash-recovery Wi-Fi re-enable failed")
    settings.save_wifi_offline_adapter(None)


def _restore_ethernet_if_pending(ctx: AppContext) -> None:
    """Finish a prior close's Ethernet→DHCP revert that failed (rc≠0).

    The X-button close reverts the primary Ethernet to DHCP; if that netsh
    call failed it persists the adapter (`ethernet_revert_pending`). On the
    next launch, if the NIC is STILL stranded on the test subnet, retry the
    DHCP revert once so the user gets their normal network back — then clear
    the marker. Gated on "still on the test subnet" so we never revert a NIC
    the user has since reconfigured (and so a one-off netsh hiccup doesn't
    fight the test workflow on every boot). Best-effort, admin-gated.
    """
    pending = settings.load_ethernet_revert_pending()
    if not pending:
        return
    from .core.fix_actions import detect_primary_adapter, is_admin
    if not is_admin():
        return  # can't revert now; leave the marker for a later elevated run
    try:
        from .core.state_capture import (
            _psutil_ipv4,
            plan_close_ethernet_revert,
            run_restore_commands,
        )

        adapter = detect_primary_adapter() or pending
        current_ip, _ = _psutil_ipv4(adapter) if adapter else (None, None)
        commands = plan_close_ethernet_revert(
            adapter,
            current_ip,
            str(ctx.config.network.server_ip),
            ctx.config.network.subnet_mask,
        )
        if commands:
            ok, _ = run_restore_commands(commands)
            ctx.logger.info(
                "crash-recovery: retried Ethernet revert (adapter=%s ip=%s) ok=%s",
                adapter, current_ip, ok,
            )
            if not ok:
                return  # keep the marker; try again next launch
        # Off the test subnet already (or reverted just now) — done.
    except Exception:  # noqa: BLE001
        ctx.logger.exception("crash-recovery Ethernet revert failed")
    settings.save_ethernet_revert_pending(None)


# Re-entrancy guard so a fault inside the crash-guard's own dialog can't
# stack a second dialog on top (which would itself fault, and so on).
_crash_guard_in_dialog = False


def install_crash_guard(logger: logging.Logger) -> None:
    """Keep the app alive (and logged) when a Qt slot or thread raises.

    Round-20 (WW): the app could *suddenly* vanish after repeatedly
    starting/stopping sweeps. PySide6 routes an unhandled exception that
    escapes a slot (our queued ``_on_event`` / ``_on_sweep_finished`` /
    ``_on_progress_tick`` handlers, hammered by rapid Run/Stop) through
    ``sys.excepthook`` — and with the *default* hook it then terminates
    the process with no traceback anywhere. Overriding ``sys.excepthook``
    both (a) writes the full traceback to the PingPair log so the real
    cause is captured, and (b) lets the event loop keep running instead
    of aborting. ``threading.excepthook`` does the same for the daemon
    reader / socket-monitor threads.
    """
    import threading
    import traceback

    def _report(
        exc_type: type[BaseException],
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        global _crash_guard_in_dialog
        try:
            detail = "".join(traceback.format_exception(exc_type, exc, tb))
            logger.error("unhandled exception (app kept alive):\n%s", detail)
        except Exception:
            # Logging itself must never re-raise from inside the guard.
            pass
        # Surface it once, non-recursively, and ONLY on the GUI thread: a
        # QMessageBox is a QWidget, and threading.excepthook fires on the
        # offending *worker* thread (the reader / socket-monitor threads),
        # where building a widget is itself a Qt cross-thread fatal the
        # try/except can't catch. Off-GUI-thread faults surface via the log
        # line above instead of a dialog.
        if _crash_guard_in_dialog:
            return
        try:
            from PySide6.QtCore import QThread
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance()
            if app is None or QThread.currentThread() is not app.thread():
                return
            _crash_guard_in_dialog = True
            QMessageBox.critical(
                None,
                "PingPair — unexpected error",
                f"{exc_type.__name__}: {exc}\n\n"
                "PingPair hit an unexpected error but stayed open. The "
                "details were written to the log "
                "(%APPDATA%\\PingPair\\logs). If this keeps happening "
                "during a sweep, please send that log.",
            )
        except Exception:
            # Never let the guard itself crash the app.
            pass
        finally:
            _crash_guard_in_dialog = False

    def _thread_report(args: threading.ExceptHookArgs) -> None:
        # A KeyboardInterrupt in a thread isn't a crash — mirror the default.
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        _report(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _report
    threading.excepthook = _thread_report


def launch_gui(ctx: AppContext, *, loopback: bool = False) -> int:
    """Create the QApplication and run the event loop. Returns exit code."""
    # High-DPI rounding must be set before the QApplication exists. PassThrough
    # keeps fractional display scales (125 % / 150 %) proportional instead of
    # rounding to whole integers — that's what makes the window, tabs, fonts
    # and input fields render at a consistent size whether the monitor is at
    # 100 % (the VM) or scaled (the host). No-op if an app already exists.
    if QApplication.instance() is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("PingPair")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("PingPair")
    app.setOrganizationDomain("pingpair.local")
    app.setWindowIcon(app_icon())

    # Last-resort crash guard: convert an unhandled slot/thread exception
    # into a logged, survivable error instead of a silent process abort
    # (Round-20 WW). Installed before any view is built so it covers the
    # whole session.
    install_crash_guard(ctx.logger)

    # Apply the saved appearance theme (Light / Dark / System) before any
    # view is built, then keep "System" in sync with later OS toggles.
    # apply_theme also re-appends the Group-F global invalid-state QSS
    # rule (the attach_* validators' dynamic-property selector), so the
    # standalone install_global_qss call it used to need is folded in.
    from . import theme as _theme
    # Swap the UI font to the bundled Inter (matches the Figma) before any
    # widget is built so every view picks it up; no-op if the assets are
    # missing. Monospace panes keep their explicit Consolas font.
    _theme.apply_ui_font(app)
    effective_theme = _theme.apply_theme(app, settings.load_theme())
    _theme.connect_system_theme(app, settings.load_theme)

    # Pull user settings from QSettings.  If a saved role is found we
    # honour it; the user can still change role from the Setup tab's
    # Role group box at any idle moment.
    settings.load_into(ctx.run_state)

    # Crash-recovery: if a prior session disabled Wi-Fi and was force-killed /
    # crashed before restoring it, re-enable that adapter now.
    _restore_wifi_if_pending(ctx)
    # Likewise finish a prior close's Ethernet→DHCP revert that failed (only
    # if the NIC is still stranded on the test subnet — see the function).
    _restore_ethernet_if_pending(ctx)

    # On first launch (no saved role + no --loopback override), sniff
    # local IPv4 addresses against the configured Server/Client IPs.
    # 192.168.1.1 -> Server, 192.168.1.2 -> Client, neither -> Loopback
    # (self-contained single-PC mode, default since 2026-06-02 - lets a
    # fresh install run a sweep with no second laptop).  --loopback is a
    # developer escape hatch that always wins.
    if not loopback and ctx.run_state.role is Role.UNDECIDED:
        role, matched = detect_role(
            server_ip=str(ctx.config.network.server_ip),
            client_ip=str(ctx.config.network.client_ip),
        )
        ctx.run_state.role = role
        # No role-mismatch warning on the no-match path: a fresh PC with
        # neither canonical IP bound opens in Loopback on purpose. The amber
        # role banner + the welcome tour explain the single-PC mode, and
        # Loopback has no canonical IP to mismatch. (A real Server/Client
        # wiring still wins via the IP hit above.)
        # Save immediately so next launch skips auto-detect entirely.
        settings.save_from(ctx.run_state)
        ctx.logger.info(
            "first-launch role auto-detect: role=%s matched=%s",
            role.value,
            matched,
        )

    # Per-launch IP/role consistency check. Even on subsequent launches
    # (saved role in QSettings), re-sniff local IPs and compare. If the
    # saved role's canonical IP isn't bound any more - e.g. the laptop
    # moved to a different LAN, or a static IP got reset - surface a
    # yellow banner on the Setup tab so the green/blue role banner at
    # the top doesn't silently lie. We never auto-flip the role here.
    # Only fills the warning when not in --loopback dev mode and only
    # when first-launch hasn't already set a message. The Setup tab
    # also re-runs this check on every prereq refresh, so the banner
    # clears automatically when the user fixes the IP.
    if not loopback and not ctx.run_state.role_warning_text:
        cfg = ctx.config.network
        warning = evaluate_role_ip_warning(
            role=ctx.run_state.role,
            bound_ips=local_ipv4_addresses(),
            server_ip=str(cfg.server_ip),
            client_ip=str(cfg.client_ip),
        )
        if warning:
            ctx.run_state.role_warning_text = warning
            ctx.logger.warning(
                "saved role=%s but canonical IP not bound; bound=%s",
                ctx.run_state.role.value,
                local_ipv4_addresses(),
            )

    # Now that the role is decided, log the full effective configuration.
    ctx.logger.info(
        "GUI starting - role=%s, server_host=%s, loopback=%s",
        ctx.run_state.role.value,
        ctx.run_state.server_host_override or ctx.config.network.server_ip,
        ctx.run_state.loopback or loopback,
    )

    # Show the loading splash first so it paints immediately, build the
    # (heavy) main window while it's up, then reveal the window when the
    # splash's timer fires (~2 s). PINGPAIR_NO_SPLASH=1 skips it entirely
    # (dev / debugger / automated launches).
    show_splash = os.environ.get("PINGPAIR_NO_SPLASH") != "1"
    splash = None
    if show_splash:
        from .views.splash import LoadingSplash

        splash = LoadingSplash(effective_theme, __version__)
        splash.show()
        app.processEvents()

    try:
        window = MainWindow(ctx, loopback=loopback or ctx.run_state.loopback)
    except BaseException:
        # If the (heavy) main-window build raises, don't leave the frameless
        # splash stranded on screen with no window and no event loop to close
        # it — tear it down before the exception propagates out of launch_gui.
        if splash is not None:
            splash.close()
            splash.deleteLater()
        raise

    if splash is not None:

        def _reveal() -> None:
            splash.close()
            splash.deleteLater()
            _run_welcome_then_show(window, ctx, effective_theme)

        splash.finished.connect(_reveal)
    else:
        _run_welcome_then_show(window, ctx, effective_theme)
    return app.exec()


def _run_welcome_then_show(
    window: MainWindow, ctx: AppContext, effective_theme: str
) -> None:
    """Run the first-boot welcome tour, THEN reveal the maximised main window.

    Round-23 (points 3 + 4): on first launch the main window stays **hidden**
    while the welcome tour is up (nothing flickers behind it), and the window
    is then shown **maximised**. Gated by the ``app/welcome_seen`` setting
    (Round-22 EEE) — the tour appears only once, then never again (it lives on
    as Help → Overview). ``PINGPAIR_NO_WELCOME=1`` opts out for dev / automated
    launches. Best-effort: a welcome failure must never stop the app starting.
    """
    show_welcome = (
        os.environ.get("PINGPAIR_NO_WELCOME") != "1"
        and not settings.load_welcome_seen()
    )
    if show_welcome:
        try:
            from .views.welcome import WelcomeDialog

            role = ctx.run_state.role.value if ctx.run_state.role else "client"
            dlg = WelcomeDialog(dark=(effective_theme == "dark"), role=role)
            dlg.exec()
        except Exception:
            # Never let a welcome-screen failure stop the app from starting.
            ctx.logger.exception("welcome screen failed to show")
        finally:
            settings.save_welcome_seen(True)
    window.showMaximized()
    window.raise_()
    window.activateWindow()

    # Feature 6: fire a throttled, opt-out auto update-check once the window
    # is up. Never reached under --check-prereqs (Qt isn't imported there)
    # and never during the welcome tour (which blocks above).
    # PINGPAIR_NO_UPDATE_CHECK=1 opts out for dev / automated launches,
    # mirroring the PINGPAIR_NO_SPLASH / PINGPAIR_NO_WELCOME idiom.
    if os.environ.get("PINGPAIR_NO_UPDATE_CHECK") != "1":
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1500, window.maybe_auto_update_check)
