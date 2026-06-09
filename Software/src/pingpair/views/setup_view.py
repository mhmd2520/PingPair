"""Setup tab: role switcher + prereq dashboard with confirm-and-fix actions.

Top of the tab is a Role group box (Server / Client / Loopback) so the
user can change which side this PC plays without restarting the app -
critical for hands-on use where the same two laptops swap roles between
runs. Switching while a sweep is in flight is refused via QMessageBox;
otherwise the change persists to QSettings and the Run tab is
rebuilt on the spot.

Below that is the prereq dashboard. Logic lives in :mod:`core.prereq`
and :mod:`core.fix_actions` so it can be unit-tested without Qt.

Threading: prereq checks include subprocess and netsh calls that can each
take ~100-500 ms.  We run them on a :class:`QThread` and emit a single
``finished(list[CheckResult])`` signal so the UI never freezes.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import settings
from ..context import AppContext, NicOverride, Role
from ..theme import ThemeMode, apply_theme
from ..core.fix_actions import (
    FIX_ACTIONS,
    FixResult,
    is_admin,
    resolve_disable_wifi,
    resolve_set_static_ip,
    run_fix,
)
from ..core.prereq import CheckResult, Status, run_checks
from ..core.state_capture import (
    capture_snapshot,
    compute_factory_reset_items,
    run_restore_commands,
)
from ..core.role_detect import (
    detect_external_ip_change,
    evaluate_role_ip_warning,
    local_ipv4_addresses,
)
from ._base import BaseView, widen_detailed_box
from ._validators import attach_ipv4_optional, attach_subnet

# Status-to-colour palette.  Tuned to read on a dark theme without blowing out.
_STATUS_COLOURS: dict[Status, tuple[str, str]] = {
    Status.PASS: ("#1f6f3a", "PASS"),
    Status.WARN: ("#a06000", "WARN"),
    Status.FAIL: ("#8b1a1a", "FAIL"),
    Status.SKIP: ("#555555", "SKIP"),
}


def _status_pill(colour: str, label: str) -> QWidget:
    """A compact rounded status pill centred in its table cell.

    Renders as a small chip (like the Figma) rather than flooding the
    whole Status cell with colour. Used via ``setCellWidget``.
    """
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(6, 2, 6, 2)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pill = QLabel(label)
    pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pill.setStyleSheet(
        f"background:{colour}; color:#ffffff; font-weight:bold;"
        " border-radius:8px; padding:2px 14px;"
    )
    lay.addWidget(pill)
    return holder


def _centered(widget: QWidget) -> QWidget:
    """Wrap ``widget`` so it sits centred (not stretched) inside a table cell.

    A cell widget is otherwise resized to fill the whole cell rect, so an
    Action-column button balloons whenever a row is momentarily tall (the
    "huge Disable Wi-Fi button" before the columns settle). Centring it in a
    holder keeps the button at its natural height regardless of row height.
    """
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(6, 3, 6, 3)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(widget)
    return holder


class _PrereqTable(QTableWidget):
    """QTableWidget that keeps row heights tight as the columns settle.

    The Detail column is word-wrapped and stretched, so each row's natural
    height depends on that column's final width. On first show the stretch
    width isn't settled yet, so the one-shot ``resizeRowsToContents()`` in
    ``_render_results`` over-estimates heights and the Action cell's button
    fills the oversized cell — the "Disable Wi-Fi button is huge until I
    switch tabs" report. Recomputing on every resize fixes it the moment the
    real geometry arrives, with no tab-flip needed.
    """

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self.rowCount():
            self.resizeRowsToContents()


# H5: a prereq pass spawns several subprocess probes (netsh / ping /
# binary -v). Tab activation calls refresh(); without a debounce, every
# flip back to the Setup tab re-spawns them. Re-activations within this
# window reuse the on-screen results instead. Re-check / role / override
# / fix / Config-Apply paths all bypass the debounce.
_PREREQ_TTL_S: float = 30.0


# "Fix all" runs fixes in this priority order (lower = earlier). Disabling
# Wi-Fi MUST precede the static-IP assignment: on a host whose Wi-Fi shares
# the test subnet (e.g. Wi-Fi on 192.168.1.x), assigning the Ethernet static
# 192.168.1.2 while Wi-Fi still owns that subnet makes Windows drop the static
# to APIPA (169.254.x.x). netsh still returns rc=0, so the IP looks "set" but
# never binds — ipconfig shows APIPA and pings fail. Clearing the subnet first
# lets the static actually take. Unlisted fixes (firewall rules) run last, in
# their original relative order (the sort is stable).
_FIX_ALL_ORDER: dict[str, int] = {"disable_wifi": 0, "set_static_ip": 1}
_FIX_ALL_DEFAULT_ORDER: int = 2


def order_fix_ids(fix_ids: list[str]) -> list[str]:
    """Stable-sort fix IDs so subnet-clearing fixes precede IP assignment.

    Single source of truth for the "Fix all" run order (see
    :data:`_FIX_ALL_ORDER`). Pure + dependency-free so the ordering contract
    is unit-tested without driving the Qt dialog.
    """
    return sorted(
        fix_ids, key=lambda f: _FIX_ALL_ORDER.get(f, _FIX_ALL_DEFAULT_ORDER)
    )


def _substantive_lines(blob: str | None) -> Iterator[str]:
    """Yield the meaningful lines of a fix's output blob.

    Strips each line and drops blanks, ``(empty)`` placeholders, and
    run_fix's combined-transcript scaffolding (``--- attempt 1 (static) ---``
    etc.). Shared by :func:`_summarize_fix_error` and
    :func:`_fix_details_useful` so "what counts as a real output line" is
    defined once.
    """
    for raw in (blob or "").splitlines():
        line = raw.strip()
        if not line or line == "(empty)":
            continue
        if line.startswith("---") and line.endswith("---"):
            continue
        yield line


def _summarize_fix_error(result: FixResult) -> str:
    """The single most-useful line from a failed fix's output, for the dialog
    summary text (so the reason is visible without expanding "Details").

    Prefers a substantive stderr line, then stdout — so for a real netsh
    failure we surface the actual error line, not a transcript header. Falls
    back to a generic pointer.
    """
    for blob in (result.stderr, result.stdout):
        for line in _substantive_lines(blob):
            return line
    return "See the details for the command output."


def _fix_details_useful(result: FixResult) -> bool:
    """True when the rc+stdout+stderr dump adds something beyond the one-line
    summary — so we don't attach a redundant "Show Details" for a single-line
    failure (e.g. the cable-unplugged refusal) or a clean, output-less success.
    """
    if (result.stdout or "").strip():
        return True
    return sum(1 for _ in _substantive_lines(result.stderr)) > 1


class _CheckWorker(QThread):
    """Runs the full check suite off the UI thread."""

    finished_with_results = Signal(list)  # list[CheckResult]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx

    def run(self) -> None:  # noqa: D401 - QThread.run override
        # Pass the saved role + per-PC NIC override so check_nic_ip
        # uses the user's customised IP (when applied) instead of the
        # profile default — single source of truth via
        # core.nic_resolve.effective_nic_for_role.
        results = run_checks(
            self.ctx.config,
            self.ctx.run_state.role,
            self.ctx.run_state.nic_override,
        )
        self.finished_with_results.emit(results)


class SetupView(BaseView):
    title = "Prerequisite checks"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        # Lightweight carrier watcher (2026-06-04): while the Setup tab is
        # visible, poll the NICs' link state every ~2 s and auto re-run the
        # prerequisites the moment it flips — a cable unplug/replug or an
        # adapter enable/disable refreshes the table within ~2 s instead of
        # waiting for a manual Re-check. Only the cheap isup snapshot runs each
        # tick; the full probe pass fires only on a detected change. Started in
        # showEvent, stopped in hideEvent / shutdown.
        self._last_link_sig: tuple[tuple[str, bool], ...] | None = None
        self._link_watch_timer = QTimer(self)
        self._link_watch_timer.setInterval(2000)
        self._link_watch_timer.timeout.connect(self._on_link_watch_tick)
        # Trigger the first check pass automatically on tab load.
        self._refresh()

    # ----- layout --------------------------------------------------------

    def _build_placeholder(self) -> None:
        """Override BaseView's placeholder with the real Setup widgets."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        outer.addWidget(QLabel(f"<h2>{self.title}</h2>"))
        setup_intro = QLabel(
            "Each row below is a prerequisite for running tests. Items in "
            "yellow or red have a Fix button; click it to review and apply "
            "the change."
        )
        setup_intro.setWordWrap(True)
        outer.addWidget(setup_intro)

        # Role group box - Server / Client / Loopback switcher.
        outer.addWidget(self._build_role_box())

        # Role/IP warning banner - shown when first-launch auto-detect
        # couldn't pick a role, or when a re-launched saved role's IP
        # isn't currently bound. Source: ``ctx.run_state.role_warning_text``.
        # Refreshed on every prereq pass via :meth:`_refresh_role_warning_banner`
        # so it clears automatically when the user fixes the IP.
        self._role_warning_banner = QLabel()
        self._role_warning_banner.setVisible(False)
        self._role_warning_banner.setWordWrap(True)
        self._role_warning_banner.setStyleSheet(
            "background:#a06000; color:#fff; padding:8px; border-radius:4px;"
        )
        outer.addWidget(self._role_warning_banner)
        if self.ctx.run_state.role_warning_text:
            self._role_warning_banner.setText(self.ctx.run_state.role_warning_text)
            self._role_warning_banner.setVisible(True)

        # Admin banner (only visible when not elevated on Windows).
        self._admin_banner = QLabel()
        self._admin_banner.setVisible(False)
        self._admin_banner.setWordWrap(True)
        self._admin_banner.setStyleSheet(
            "background:#a06000; color:#fff; padding:8px; border-radius:4px;"
        )
        outer.addWidget(self._admin_banner)

        # Top control row.
        control_row = QHBoxLayout()
        self._status_summary = QLabel("Running checks...")
        control_row.addWidget(self._status_summary, stretch=1)

        self._fix_all_btn = QPushButton("Fix all")
        self._fix_all_btn.setToolTip(
            "Run every Fix action in the table at once, in sequence. "
            "Failures don't stop the others — a summary appears at the end."
        )
        self._fix_all_btn.setEnabled(False)
        self._fix_all_btn.clicked.connect(self._on_fix_all)
        control_row.addWidget(self._fix_all_btn)

        self._recheck_btn = QPushButton("Re-check")
        self._recheck_btn.clicked.connect(self._refresh)
        control_row.addWidget(self._recheck_btn)
        outer.addLayout(control_row)

        # Results table.
        self._table = _PrereqTable(0, 4)
        self._table.setHorizontalHeaderLabels(["Status", "Check", "Detail", "Action"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        # Round-21 (AAA): one-line details. Word-wrap off means each Detail
        # cell renders on a single line and elides with "…" when it doesn't
        # fit; the full sentence stays available on hover (the per-cell
        # tooltip set in _render_results). Keeps every row a uniform single
        # line instead of the 2-3 line rows the wrapped Wi-Fi / Ethernet
        # details produced.
        self._table.setWordWrap(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        outer.addWidget(self._table, stretch=1)

        # Appearance (theme) + app-wide factory reset, side by side below
        # the prereq table. Both moved here from the removed Preferences tab.
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addWidget(self._build_appearance_box(), stretch=1)
        bottom.addWidget(self._build_reset_box(), stretch=1)
        outer.addLayout(bottom)

        self._worker: _CheckWorker | None = None
        # H5/M8 prereq-check throttling. ``_last_check_done`` gates the
        # tab-activation path; ``_refresh_pending`` coalesces a forced
        # re-run requested while a check is already in flight so a
        # role/override/fix change is never lost, yet workers never stack.
        self._last_check_done: float = 0.0
        self._refresh_pending: bool = False

    # ----- appearance / theme ------------------------------------------

    def _build_appearance_box(self) -> QGroupBox:
        """Light / Dark / System theme selector. Applies live on change."""
        box = QGroupBox("Appearance")
        col = QVBoxLayout(box)
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme:"))
        self._theme_combo = QComboBox()
        for label, mode in (
            ("System", ThemeMode.SYSTEM),
            ("Light", ThemeMode.LIGHT),
            ("Dark", ThemeMode.DARK),
        ):
            self._theme_combo.addItem(label, mode.value)
        idx = self._theme_combo.findData(settings.load_theme().value)
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._theme_combo.setToolTip(
            "System follows the OS light/dark setting and live-updates if "
            "you toggle it. Applies immediately — no restart."
        )
        theme_row.addWidget(self._theme_combo, stretch=1)
        col.addLayout(theme_row)
        hint = QLabel(
            "System follows the OS light/dark setting and live-updates. "
            "Applies immediately — no restart."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888;")
        col.addWidget(hint)

        # Round-6 #7: notification-sound toggle. Plays the user's own Windows
        # system sounds on sweep finish / failure / error / prompt; off here
        # silences them. Lives in this preferences box next to the theme.
        self._sounds_checkbox = QCheckBox("Play notification sounds")
        self._sounds_checkbox.setChecked(settings.load_sounds_enabled())
        self._sounds_checkbox.setToolTip(
            "Play a short Windows system sound when a sweep finishes, fails, "
            "errors out, or a dialog needs your input. Uses your own Windows "
            "sound scheme — no custom audio is bundled."
        )
        self._sounds_checkbox.toggled.connect(self._on_sounds_toggled)
        col.addWidget(self._sounds_checkbox)
        col.addStretch(1)
        return box

    def _on_theme_changed(self) -> None:
        mode = ThemeMode.coerce(self._theme_combo.currentData())
        settings.save_theme(mode)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, mode)

    def _on_sounds_toggled(self, checked: bool) -> None:
        settings.save_sounds_enabled(checked)

    # ----- reset all settings ------------------------------------------

    def _build_reset_box(self) -> QGroupBox:
        """App-wide factory reset (moved from the former Preferences tab).

        Clears QSettings preferences AND reverts the network changes the
        app makes (firewall rules, IP, Wi-Fi) — captured live from the
        host at click time — so one click leaves the host the way a
        fresh install would find it.
        """
        box = QGroupBox("Reset")
        row = QHBoxLayout(box)
        note = QLabel(
            "Leave this PC the way it looked before PingPair was ever "
            "installed: clear every saved preference (role, report "
            "settings, metadata, sweep subset, NIC overrides, window "
            "size), remove the PingPair firewall rules, revert "
            "Ethernet to DHCP, and re-enable Wi-Fi. Saved test-plan "
            "profiles and reports are not touched."
        )
        note.setWordWrap(True)
        row.addWidget(note, stretch=1)
        self._reset_btn = QPushButton("Reset all settings")
        self._reset_btn.clicked.connect(self._on_reset_all)
        row.addWidget(self._reset_btn)
        return box

    def _gather_reset_restore_items(self) -> list:
        """Return the factory-wipe restore items, or ``[]`` on capture error.

        Reset's model is "uninstall + reinstall" — remove every firewall
        rule PingPair owns, revert Ethernet to DHCP, re-enable Wi-Fi —
        not "rewind to launch state." See
        :func:`compute_factory_reset_items` for the rationale.
        """
        try:
            current = capture_snapshot()
        except Exception:  # noqa: BLE001 - never let capture break Reset
            return []
        return compute_factory_reset_items(current)

    def _on_reset_all(self) -> None:
        """Confirm, undo PingPair's network changes (if any), clear
        QSettings, then close the app so the wiped store isn't immediately
        re-persisted on exit."""
        network_items = self._gather_reset_restore_items()
        network_changed = bool(network_items)
        network_can_restore = network_changed and is_admin()

        info_lines = [
            "Saved preferences will be cleared: role, report settings, "
            "test-record metadata, sweep subset, NIC overrides, window size."
        ]
        if network_can_restore:
            info_lines.append("")
            info_lines.append(
                "PingPair will also leave the host the way a fresh install "
                "would find it:"
            )
            for it in network_items:
                info_lines.append(f"  - {it.label}")
        elif network_changed:
            info_lines.append("")
            info_lines.append(
                "WARNING: PingPair would normally also revert firewall / "
                "Ethernet / Wi-Fi state to factory defaults, but this "
                "session isn't running as Administrator, so the network "
                "side will NOT be reset."
            )
        info_lines.append("")
        info_lines.append(
            "Saved test-plan profiles and reports are not touched. "
            "This cannot be undone."
        )

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Reset all settings?")
        confirm.setText(
            "Reset PingPair to a fresh-install state on this PC?"
        )
        confirm.setInformativeText("\n".join(info_lines))
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        # Undo network changes first so a partial failure still leaves
        # the QSettings reset intact below.
        transcript = ""
        restore_ok = True
        if network_can_restore:
            commands = [cmd for item in network_items for cmd in item.commands]
            restore_ok, transcript = run_restore_commands(commands)
            self.ctx.logger.info(
                "reset-all: network restore ok=%s, %d command(s)",
                restore_ok, len(commands),
            )

        settings.reset_all()
        self.ctx.logger.info("all settings reset to defaults via the Setup tab")

        done = QMessageBox(self)
        done.setIcon(
            QMessageBox.Icon.Information if restore_ok else QMessageBox.Icon.Warning
        )
        done.setWindowTitle("Settings reset")
        summary = ["All settings have been reset to defaults."]
        if network_can_restore:
            summary.append(
                "Firewall rules removed, Ethernet → DHCP, Wi-Fi re-enabled."
                if restore_ok
                else "Some factory-reset commands did not succeed — see details."
            )
        elif network_changed:
            summary.append(
                "Firewall / Ethernet / Wi-Fi were NOT reset (admin required)."
            )
        summary.append("")
        summary.append("PingPair will now close — reopen it to start fresh.")
        done.setText("\n".join(summary))
        if transcript:
            done.setDetailedText(transcript)
            widen_detailed_box(done)
        done.exec()

        win = self.window()
        if win is not None:
            win._suppress_close_save = True  # type: ignore[attr-defined]
            win.close()

    # ----- role switcher -----------------------------------------------

    def _build_role_box(self) -> QGroupBox:
        """Build the Role group box.

        Group F (Q1, 2026-05-16): radios are horizontal and **auto-apply
        on toggle** — no Apply button. The mid-sweep guard reverts the
        radio selection via :attr:`_suppress_role_toggle` so the user
        sees the refusal but doesn't end up with a stale radio state.
        """
        box = QGroupBox("Role - which side does this PC play?")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        cfg = self.ctx.config.network
        # Short radio labels — full descriptions live in tooltips so the
        # horizontal row stays scannable on a 1080p screen.
        self._server_radio = QRadioButton("Server")
        self._client_radio = QRadioButton("Client")
        self._loopback_radio = QRadioButton("Loopback")
        self._server_radio.setToolTip(
            f"Server  -  bind to {cfg.server_ip}, listen on TCP "
            f"{cfg.control_port} (control) and 5201 (iperf3)"
        )
        self._client_radio.setToolTip(
            f"Client  -  bind to {cfg.client_ip}, drive the 20-case "
            f"sweep against the Server"
        )
        self._loopback_radio.setToolTip(
            "Loopback (dev / single PC)  -  Server and Client both on 127.0.0.1"
        )

        # Reflect the currently-active role in the radios.
        current = self.ctx.run_state.role
        if current is Role.SERVER:
            self._server_radio.setChecked(True)
        elif current is Role.LOOPBACK:
            self._loopback_radio.setChecked(True)
        else:
            self._client_radio.setChecked(True)

        radio_group = QButtonGroup(box)
        radio_group.addButton(self._server_radio)
        radio_group.addButton(self._client_radio)
        radio_group.addButton(self._loopback_radio)

        # Horizontal radio row.
        radio_row = QHBoxLayout()
        radio_row.addWidget(self._server_radio)
        radio_row.addWidget(self._client_radio)
        radio_row.addWidget(self._loopback_radio)
        radio_row.addStretch(1)
        layout.addLayout(radio_row)

        # Auto-apply wiring. Each radio's toggled signal fires twice on
        # a user click (False on the previously-checked one + True on
        # the newly-checked one). _on_role_toggled filters for the
        # True transition and ignores programmatic reverts via the
        # _suppress_role_toggle guard.
        self._suppress_role_toggle = False
        self._server_radio.toggled.connect(self._on_role_toggled)
        self._client_radio.toggled.connect(self._on_role_toggled)
        self._loopback_radio.toggled.connect(self._on_role_toggled)

        # Client-only Server-host override row (still relevant — this
        # tells the Client where the Server lives, separate from the
        # NIC override fields added in task #22).
        host_row = QHBoxLayout()
        host_row.addSpacing(20)
        self._host_label = QLabel("Server host:")
        self._host_input = QLineEdit()
        self._host_input.setPlaceholderText(str(cfg.server_ip))
        # Live red-border feedback for invalid IPv4 input (Group F
        # follow-up, 2026-05-17). Pydantic on Apply remains authoritative.
        attach_ipv4_optional(
            self._host_input,
            "Remote Server IP (IPv4 dotted-quad). "
            "Blank = use the profile default 192.168.1.1.",
        )
        if self.ctx.run_state.server_host_override:
            self._host_input.setText(self.ctx.run_state.server_host_override)
        # Editing the Server host while in Client role should also
        # auto-apply once the user finishes (editingFinished fires on
        # focus-out / Enter — debounce-friendly).
        self._host_input.editingFinished.connect(self._on_host_edited)
        host_row.addWidget(self._host_label)
        host_row.addWidget(self._host_input, stretch=1)
        layout.addLayout(host_row)

        # Sync enabled state of host row to the Client radio's state.
        self._client_radio.toggled.connect(self._sync_host_enabled)
        self._sync_host_enabled()

        # ----- Override section (Q1 task #22, 2026-05-16) -----
        # Master checkbox gates the three input fields. Unticked = fields
        # greyed and disabled, profile defaults apply. Ticked = fields
        # editable, debounced auto-apply on edit (500 ms after last
        # keystroke) pushes values into RunState.nic_override.
        cfg_net = self.ctx.config.network
        ov = self.ctx.run_state.nic_override
        self._override_checkbox = QCheckBox(
            "Use a custom IP configuration  (overrides the defaults below)"
        )
        self._override_checkbox.setChecked(ov.use_custom)
        self._override_checkbox.setToolTip(
            "When ticked, the IP / Subnet / Gateway fields below override "
            "the profile defaults for THIS PC only. The other PC's defaults "
            "are unaffected — both PCs can still share the same profile."
        )
        layout.addWidget(self._override_checkbox)

        # Three input fields, horizontal. Placeholders carry the role
        # defaults so the user knows what they'd get if they left a
        # field blank.
        override_row = QHBoxLayout()
        override_row.addSpacing(20)
        override_row.addWidget(QLabel("IP:"))
        self._override_ip = QLineEdit()
        # Role-aware placeholder: only the IP this PC's current role would
        # bind to, not both sides separated by a slash. Updated on every
        # role toggle via _refresh_override_placeholders().
        self._override_ip.setPlaceholderText(self._role_default_ip_str())
        attach_ipv4_optional(
            self._override_ip,
            "This PC's NIC IP (IPv4 dotted-quad). Blank = profile default.",
        )
        if ov.ip:
            self._override_ip.setText(ov.ip)
        override_row.addWidget(self._override_ip, stretch=2)

        override_row.addWidget(QLabel("Subnet:"))
        self._override_subnet = QLineEdit()
        self._override_subnet.setPlaceholderText(str(cfg_net.subnet_mask))
        attach_subnet(self._override_subnet)
        if ov.subnet:
            self._override_subnet.setText(ov.subnet)
        override_row.addWidget(self._override_subnet, stretch=2)

        override_row.addWidget(QLabel("Gateway:"))
        self._override_gateway = QLineEdit()
        gw_default = (
            str(cfg_net.gateway) if cfg_net.gateway else "(none)"
        )
        self._override_gateway.setPlaceholderText(gw_default)
        attach_ipv4_optional(
            self._override_gateway,
            "Default gateway IP (IPv4). Blank = no gateway (point-to-point LAN).",
        )
        if ov.gateway:
            self._override_gateway.setText(ov.gateway)
        override_row.addWidget(self._override_gateway, stretch=2)
        layout.addLayout(override_row)

        # Debounce timer — restarts on every keystroke, fires apply
        # 500 ms after the user stops typing. Avoids prereq-table
        # flicker while the user is mid-typing an IP.
        self._override_debounce = QTimer(self)
        self._override_debounce.setSingleShot(True)
        self._override_debounce.setInterval(500)
        self._override_debounce.timeout.connect(self._apply_nic_override)

        # Wire signals. Checkbox toggle is immediate (enable/disable +
        # apply); field edits debounced.
        self._override_checkbox.toggled.connect(self._on_override_checkbox_toggled)
        self._override_ip.textChanged.connect(self._on_override_field_changed)
        self._override_subnet.textChanged.connect(self._on_override_field_changed)
        self._override_gateway.textChanged.connect(self._on_override_field_changed)

        # Sync field-enabled state with the checkbox.
        self._sync_override_fields_enabled()

        # Re-sync the Server-host visibility now that the override
        # checkbox exists — the initial _sync_host_enabled() call (right
        # after the host row is built) fired before _override_checkbox
        # was created, so it always evaluated override_on=False. Re-run
        # here so a persisted "override ON" state on launch shows the
        # field correctly on first paint.
        self._sync_host_enabled()

        # Status line — no Apply button anymore (auto-apply on toggle).
        self._role_status = QLabel(self._render_role_summary())
        self._role_status.setStyleSheet("color:#aaa;")
        layout.addWidget(self._role_status)

        return box


    def _selected_role(self) -> Role:
        if self._server_radio.isChecked():
            return Role.SERVER
        if self._loopback_radio.isChecked():
            return Role.LOOPBACK
        return Role.CLIENT

    def _sync_host_enabled(self) -> None:
        """Show / hide the Server host row.

        Pre-Group-F this only enabled / disabled the field. Per Mohamed's
        Group F follow-up (2026-05-17), the field is now **hidden** when
        not needed so the canonical point-to-point setup collapses to a
        cleaner Setup tab:

        - Server role / Loopback role: hidden (those don't dial a remote
          target).
        - Client role + override OFF: hidden (Client uses the profile's
          canonical server_ip = 192.168.1.1; no reason to show it).
        - Client role + override ON: shown so the user can target a
          non-canonical Server IP.

        The override-checkbox is built after this row, so we tolerate it
        being absent during the initial _build_placeholder call.
        """
        is_client = self._client_radio.isChecked()
        override_on = (
            getattr(self, "_override_checkbox", None) is not None
            and self._override_checkbox.isChecked()
        )
        visible = is_client and override_on
        self._host_label.setVisible(visible)
        self._host_input.setVisible(visible)
        # Keep setEnabled in sync so any code path that reads enabled-state
        # (e.g. validators, focus traversal) sees consistent state.
        self._host_label.setEnabled(visible)
        self._host_input.setEnabled(visible)

    def _render_role_summary(self) -> str:
        rs = self.ctx.run_state
        if rs.role is Role.SERVER:
            return f"Active role: Server (listening on {self.ctx.config.network.server_ip})"
        if rs.role is Role.CLIENT:
            host = rs.server_host_override or str(self.ctx.config.network.server_ip)
            return f"Active role: Client (Server host = {host})"
        if rs.role is Role.LOOPBACK:
            return "Active role: Loopback (127.0.0.1)"
        return "Active role: undecided"

    def _on_role_toggled(self, checked: bool) -> None:
        """Auto-apply when a radio becomes checked.

        Only the True transition triggers the apply path; the False
        transition (on the radio that's being unchecked) is ignored.
        Programmatic reverts (e.g. after a refused mid-sweep change)
        set ``_suppress_role_toggle`` so this handler ignores them.
        """
        if not checked:
            return
        if getattr(self, "_suppress_role_toggle", False):
            return
        self._apply_role()

    def _on_host_edited(self) -> None:
        """Auto-apply when the Server-host field finishes editing.

        Only relevant in Client mode — _sync_host_enabled disables the
        input otherwise. editingFinished fires on focus-out and Enter
        so we don't try to apply mid-typing.
        """
        if self._client_radio.isChecked():
            self._apply_role()

    def _apply_role(self) -> None:
        """Persist a new role choice and rebuild the Run tab in place.

        Group F (Q1, 2026-05-16): renamed from _on_apply_role — the
        Apply button is gone, this is now called automatically by
        :meth:`_on_role_toggled` and :meth:`_on_host_edited`. The
        mid-sweep guard reverts the radio selection so the user
        doesn't end up with a stale view.
        """
        rs = self.ctx.run_state
        new_role = self._selected_role()
        new_host = self._host_input.text().strip() or None

        # No-op shortcut.
        no_change = (
            new_role == rs.role
            and (new_host or None) == (rs.server_host_override or None)
        )
        if no_change:
            return

        # Refuse mid-sweep changes. Revert the radio + host input so the
        # user sees the rejection reflected in the UI immediately.
        if rs.sweep_active:
            QMessageBox.warning(
                self,
                "Sweep in progress",
                "A sweep is currently running. Stop or wait for it to finish "
                "before changing the role on this PC.",
            )
            self._suppress_role_toggle = True
            try:
                if rs.role is Role.SERVER:
                    self._server_radio.setChecked(True)
                elif rs.role is Role.LOOPBACK:
                    self._loopback_radio.setChecked(True)
                else:
                    self._client_radio.setChecked(True)
                # Restore the host input too, in case it was the trigger.
                self._host_input.setText(rs.server_host_override or "")
            finally:
                self._suppress_role_toggle = False
            return

        # Apply the change.
        rs.role = new_role
        rs.loopback = (new_role is Role.LOOPBACK)
        rs.server_host_override = new_host if new_role is Role.CLIENT else None
        settings.save_from(rs)

        # Re-evaluate the role/IP banner against the NEW role.
        self._refresh_role_warning_banner()

        # Flip the override-row IP placeholder to the new role's default.
        self._refresh_override_placeholders()

        # Rebuild the Run tab and refresh the role banner.
        window = self.window()
        if hasattr(window, "rebuild_script_tab"):
            window.rebuild_script_tab()
        if hasattr(window, "refresh_role_banner"):
            window.refresh_role_banner()

        self._role_status.setText(self._render_role_summary() + "  ·  applied.")
        self.ctx.logger.info(
            "role auto-applied: role=%s, server_host=%s",
            new_role.value,
            new_host or self.ctx.config.network.server_ip,
        )

        # Re-run prereqs so Local NIC IP re-evaluates against the new role.
        self._refresh()


    def _sync_override_fields_enabled(self) -> None:
        """Enable/disable the 3 override fields based on the checkbox.

        When the checkbox is unticked the fields are greyed; on the
        UNtick *transition* _on_override_checkbox_toggled also wipes
        their text so a stale typo doesn't linger. The effective
        config falls back to profile defaults while unticked
        (handled by effective_nic_for_role downstream).
        """
        enabled = self._override_checkbox.isChecked()
        for w in (self._override_ip, self._override_subnet, self._override_gateway):
            w.setEnabled(enabled)

    def _role_default_ip_str(self) -> str:
        """The IP this PC would bind to under the current role + profile.

        Server -> profile's server_ip (canonical 192.168.1.1).
        Client -> profile's client_ip (canonical 192.168.1.2).
        Loopback -> 127.0.0.1 (both ends share the loopback address).
        Anything else (Undecided) -> server_ip as the safest fallback.

        Used as the IP placeholder in the override row so the user sees
        only the IP relevant to THIS PC, not both sides slash-separated.
        """
        cfg_net = self.ctx.config.network
        role = self.ctx.run_state.role
        if role is Role.SERVER:
            return str(cfg_net.server_ip)
        if role is Role.CLIENT:
            return str(cfg_net.client_ip)
        if role is Role.LOOPBACK:
            return "127.0.0.1"
        return str(cfg_net.server_ip)

    def _refresh_override_placeholders(self) -> None:
        """Re-render the override-row placeholders against the current role.

        Called after every role toggle so the IP placeholder flips between
        the Server / Client / Loopback canonical value without needing an
        app restart. Subnet + Gateway placeholders are role-agnostic so
        they're left as-is (Subnet always reads the profile mask, Gateway
        reads the profile gateway or "(none)").
        """
        if not hasattr(self, "_override_ip"):
            return  # Setup view still building.
        self._override_ip.setPlaceholderText(self._role_default_ip_str())

    def _on_override_checkbox_toggled(self, checked: bool) -> None:
        """Toggle the override checkbox.

        Applies immediately (no debounce) since this is a definite
        user intent, not mid-typing. Also re-syncs the field-enabled
        state.

        Group F follow-up (2026-05-17): when the user UNticks the
        checkbox we *clear* the three fields rather than just
        greying them. A stale typo (e.g. "1991155" left in the IP
        field) would otherwise keep flagging red even though the
        override is disabled, and re-ticking the box would restore
        the bad value. Clearing on untick gives the user a clean
        reset to profile defaults.
        """
        if not checked:
            # Wipe stale text + red borders. textChanged fires per
            # setText("") so each validator paints itself valid
            # (empty + allow_blank=True for the three override fields).
            for w in (
                self._override_ip,
                self._override_subnet,
                self._override_gateway,
            ):
                w.setText("")
        self._sync_override_fields_enabled()
        # Server host visibility depends on override state — refresh
        # it here so the field shows / hides in lockstep with the
        # checkbox.
        self._sync_host_enabled()
        self._apply_nic_override()

    def _on_override_field_changed(self, _text: str = "") -> None:
        """Restart the debounce timer on every keystroke in the 3 fields.

        Skipped when the checkbox is unticked — typing into a disabled
        field shouldn't happen (Qt blocks it), but defensive guard for
        programmatic setText() calls.
        """
        if not self._override_checkbox.isChecked():
            return
        self._override_debounce.start()

    def _apply_nic_override(self) -> None:
        """Push the current override widget values into RunState.

        Called by the checkbox toggle (immediate) and the debounce
        timer (500 ms after the last field keystroke). Persists to
        QSettings + fires the prereq re-check so the Local NIC IP row
        re-evaluates against the new effective IP. Mid-sweep is NOT
        blocked here — the user may want to type in the override
        ahead of stopping the sweep; only role changes are blocked.
        """
        rs = self.ctx.run_state
        # Build the new override snapshot. Empty fields become None so
        # downstream effective_nic_for_role falls back to the profile
        # per-field.
        new_override = NicOverride(
            use_custom=self._override_checkbox.isChecked(),
            ip=self._override_ip.text().strip() or None,
            subnet=self._override_subnet.text().strip() or None,
            gateway=self._override_gateway.text().strip() or None,
        )
        if new_override == rs.nic_override:
            return
        rs.nic_override = new_override
        settings.save_from(rs)
        self.ctx.logger.info(
            "NIC override auto-applied: use_custom=%s ip=%s subnet=%s gateway=%s",
            new_override.use_custom,
            new_override.ip or "(default)",
            new_override.subnet or "(default)",
            new_override.gateway or "(default)",
        )
        # Re-run prereqs so the Local NIC IP row reflects the new
        # effective IP. Also refresh the role-warning banner since the
        # override affects what "the right IP for this role" means.
        self._refresh_role_warning_banner()
        self._refresh()

    # ----- behaviour -----------------------------------------------------

    def refresh(self) -> None:
        """Tab-activation entry point — called by the main window.

        Debounced (H5): a prereq pass spawns several subprocess probes,
        so flipping between tabs shouldn't re-run them every time. Skips
        the re-run when the last completed pass is younger than
        :data:`_PREREQ_TTL_S` (or one is already in flight). The Re-check
        button, role / override / fix paths, and the Config tab Apply all
        bypass this via :meth:`_refresh` / :meth:`force_refresh`.
        """
        if self._worker is not None:
            try:
                if self._worker.isRunning():
                    return  # in-flight pass will render fresh results
            except RuntimeError:
                self._worker = None
        if self._last_check_done and (
            time.monotonic() - self._last_check_done < _PREREQ_TTL_S
        ):
            return
        self._refresh()

    def force_refresh(self) -> None:
        """Unconditional prereq re-run — used after a Config tab Apply
        changes the network IPs. Bypasses the tab-activation debounce."""
        self._refresh()

    # ----- live carrier watch ------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        """Start the carrier watcher when the Setup tab becomes visible.

        Seeds the baseline from the current link state so the first tick
        doesn't false-trigger on a state that was already settled while the
        tab was hidden.
        """
        super().showEvent(event)
        self._last_link_sig = self._sample_link_signature()
        self._link_watch_timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        """Stop the carrier watcher when the tab is hidden — no background
        polling while another tab is in front."""
        super().hideEvent(event)
        self._link_watch_timer.stop()

    @staticmethod
    def _sample_link_signature() -> tuple[tuple[str, bool], ...] | None:
        """Cheap carrier snapshot of every NIC, or None if unavailable.

        One ``psutil.net_if_stats()`` syscall — no netsh / firewall probes,
        unlike a full prereq pass. Returns a hashable ``(iface, isup)``
        signature so a cable unplug/replug or an adapter enable/disable flips
        it. Best-effort: returns None on any failure, so the watcher simply
        does nothing that tick.
        """
        try:
            import psutil  # lazy so non-Windows tests can monkeypatch
        except ImportError:
            return None
        try:
            stats = psutil.net_if_stats()
        except Exception:  # noqa: BLE001 - psutil can raise on odd NICs
            return None
        return tuple(sorted((name, bool(st.isup)) for name, st in stats.items()))

    def _on_link_watch_tick(self) -> None:
        """Re-run the prereqs when a NIC's carrier state flips (see __init__).

        Only the snapshot runs each tick; the full probe pass
        (:meth:`force_refresh`) runs only on a detected change, and
        :meth:`_refresh` coalesces if a pass is already in flight — so a
        flapping link can't spawn a worker storm.
        """
        sig = self._sample_link_signature()
        if sig is None:
            return
        prev = self._last_link_sig
        self._last_link_sig = sig
        # Skip while a prereq pass OR a Fix-all is already running. Fix-all
        # pumps the event loop with processEvents() between netsh fixes, and a
        # fix like "Disable Wi-Fi" flips a NIC's carrier — so without this
        # guard the tick would fire force_refresh() mid-batch, spawning a
        # concurrent _CheckWorker whose _on_checks_finished re-enables the
        # buttons while Fix-all is still running. Re-check being disabled is
        # the single "busy" signal for both states. We've already absorbed the
        # new signature into the baseline above, and the busy operation ends
        # with its own refresh, so nothing is missed.
        recheck = getattr(self, "_recheck_btn", None)
        if recheck is not None and not recheck.isEnabled():
            return
        if prev is not None and sig != prev:
            self.ctx.logger.info(
                "Setup: NIC link state changed — auto re-running prerequisites"
            )
            self.force_refresh()

    def shutdown(self) -> None:
        """Wait for the prereq-check worker before the app tears the view down.

        Closing the app mid-check would otherwise destroy the running
        ``_CheckWorker`` QThread, which makes Qt abort with "QThread:
        Destroyed while thread is still running" — a source of the
        crash-on-close. Called by :meth:`MainWindow.closeEvent`.
        """
        # Stop the carrier watcher first so a tick can't kick off a fresh
        # prereq worker while we're tearing the view down.
        timer = getattr(self, "_link_watch_timer", None)
        if timer is not None:
            timer.stop()
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                # A full prereq pass (firewall + IP + tool probes) can take
                # 20-25 s on a cold machine. The wait ceiling must clear that
                # worst case, or a close mid-check still hits the "QThread
                # destroyed while running" abort the rest of this method
                # exists to prevent.
                worker.wait(30000)
        except RuntimeError:
            pass  # libshiboken: underlying C++ object already deleted

    def _refresh(self) -> None:
        """Spawn a prereq-check pass (always forced).

        M8: if a check is already in flight, don't stack a second worker
        — set ``_refresh_pending`` so :meth:`_on_checks_finished` re-runs
        once more with the latest config. That keeps a role/override/fix
        change from being lost without ever running two workers at once.
        """
        if self._worker is not None:
            try:
                if self._worker.isRunning():
                    self._refresh_pending = True
                    return
            except RuntimeError:
                self._worker = None
        self._refresh_pending = False
        self._update_admin_banner()
        self._status_summary.setText("Running checks...")
        self._recheck_btn.setEnabled(False)
        if hasattr(self, "_fix_all_btn"):
            self._fix_all_btn.setEnabled(False)

        worker = _CheckWorker(self.ctx)
        worker.finished_with_results.connect(self._on_checks_finished)
        # Round-21 (YY): drop our reference on the *built-in* finished signal
        # (fires only after the QThread has truly terminated), NOT on
        # finished_with_results — run() emits that as its last line while the
        # thread is still alive. Nulling the reference in the custom-signal
        # handler opened a window where a close mid-window made shutdown()
        # skip its wait() and Qt aborted with "QThread: Destroyed while
        # thread is still running". Mirrors the _ServerPanel pattern.
        worker.finished.connect(self._on_worker_thread_finished)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _update_admin_banner(self) -> None:
        if is_admin():
            self._admin_banner.setVisible(False)
            return
        self._admin_banner.setText(
            "Some prerequisite fixes require Administrator privileges, but this "
            "session isn't elevated. Close PingPair and relaunch it as "
            "Administrator (right-click → Run as administrator) to enable the Fix buttons."
        )
        self._admin_banner.setVisible(True)

    def _on_checks_finished(self, results: list[CheckResult]) -> None:
        # Round-21 (YY): do NOT clear self._worker here. This slot is wired to
        # the worker's custom finished_with_results signal, which
        # _CheckWorker.run() emits as its *last line* — before the QThread has
        # actually terminated. The reference is dropped in
        # _on_worker_thread_finished (the built-in finished signal), which
        # fires only once the thread is truly dead, so a close in this window
        # still finds a worker for shutdown() to wait on.
        self._last_check_done = time.monotonic()
        self._last_results = results
        self._render_results(results)
        summary = self._summarise(results)
        self._status_summary.setText(summary)
        self._recheck_btn.setEnabled(True)
        # Fix-all is meaningful only when at least one row has a fix.
        # Excludes restart_as_admin — that closes the app via UAC and
        # would derail the rest of the batch.
        fixable = self._fixable_results(results)
        self._fix_all_btn.setEnabled(bool(fixable))
        # Re-evaluate the role/IP mismatch banner against the current
        # bound IPs every time the prereq table finishes a pass. This
        # is what makes the banner disappear after the user fixes the
        # NIC IP via the "Set the correct IP" button.
        self._refresh_role_warning_banner()

        # Group F task #23: check for external IP changes after every
        # prereq pass — the bound IPs were just re-enumerated by
        # check_nic_ip and the table reflects them now.
        self._check_external_ip_change()

        # NOTE: the M8 forced-re-run (a role / override / fix change requested
        # while this pass was in flight) is honoured in
        # _on_worker_thread_finished — i.e. only after the old thread has
        # fully exited — so two _CheckWorkers never overlap. (Round-21 YY.)

    @Slot()
    def _on_worker_thread_finished(self) -> None:
        """Built-in ``QThread.finished`` — the OS thread has actually exited.

        Only here is it safe to drop our reference to the worker (see the
        note in :meth:`_on_checks_finished` for why the custom-signal handler
        must not). A stale ``finished`` from a worker we've already replaced
        is ignored via the sender guard — mirrors
        :meth:`_ServerPanel._on_worker_finished`. (Round-21 YY.)
        """
        self._clear_finished_worker(self.sender())

    def _clear_finished_worker(self, finished_worker: object) -> None:
        """Drop ``self._worker`` iff ``finished_worker`` is the live one.

        Split out from :meth:`_on_worker_thread_finished` so the sender-guard
        and pending-re-run logic is unit-testable without emitting a real Qt
        signal.
        """
        if finished_worker is not self._worker:
            return  # stale finished from a worker we've already replaced
        self._worker = None
        # M8: a forced re-run requested while the (now-finished) pass was in
        # flight. Start it now that the old thread is truly gone.
        if self._refresh_pending:
            self._refresh_pending = False
            self._refresh()

    def _check_external_ip_change(self) -> None:
        """Pop the External-IP-change dialog when the bound NIC IP diverges
        from the last-applied baseline.

        Group F (Q1 task #23, 2026-05-16). Called after every prereq pass;
        the pure detection logic lives in
        :func:`core.role_detect.detect_external_ip_change` so it's
        unit-testable without Qt.

        Anti-loop: 30 s cooldown after the user cancels, so a "Cancel"
        click doesn't immediately re-trigger on the next prereq tick.
        """
        now = time.monotonic()
        cooldown_until = getattr(self, "_external_change_cooldown", 0.0)
        if now < cooldown_until:
            return

        from ..core.nic_resolve import effective_nic_for_role

        rs = self.ctx.run_state
        eff = effective_nic_for_role(rs.role, self.ctx.config, rs.nic_override)
        bound = local_ipv4_addresses()
        last_applied = rs.last_applied_ip.get(rs.role)
        divergence = detect_external_ip_change(
            rs.role, eff.ip, bound, last_applied
        )
        if divergence is None:
            return
        was, now_ip = divergence
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("External IP change detected")
        box.setText(
            f"This PC was previously bound to <b>{was}</b> but is now "
            f"bound to <b>{now_ip}</b>."
        )
        box.setInformativeText(
            "The IP changed outside PingPair. Did you intend this?"
        )
        keep_btn = box.addButton(
            "Keep new IP", QMessageBox.ButtonRole.AcceptRole
        )
        restore_btn = box.addButton(
            "Restore previous", QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is keep_btn:
            rs.last_applied_ip[rs.role] = now_ip
            if rs.nic_override.use_custom:
                rs.nic_override = NicOverride(
                    use_custom=True,
                    ip=now_ip,
                    subnet=rs.nic_override.subnet,
                    gateway=rs.nic_override.gateway,
                )
                if hasattr(self, "_override_ip"):
                    # Block textChanged so this programmatic sync doesn't
                    # kick the debounce timer into a redundant
                    # _apply_nic_override — the override was already
                    # updated above. (The old _suppress_role_toggle guard
                    # here was a no-op: that flag is only read by the
                    # role-radio handler, not _on_override_field_changed.)
                    self._override_ip.blockSignals(True)
                    try:
                        self._override_ip.setText(now_ip)
                    finally:
                        self._override_ip.blockSignals(False)
            settings.save_from(rs)
            self.ctx.logger.info(
                "External IP change accepted: %s -> %s for role=%s",
                was, now_ip, rs.role.value,
            )
        elif clicked is restore_btn:
            self.ctx.logger.info(
                "External IP change rejected — restoring %s for role=%s",
                was, rs.role.value,
            )
            # Re-apply the role's canonical static IP. The fix_id is the
            # registry key "set_static_ip" — run_fix resolves the role +
            # adapter argv itself. (This previously passed resolved.label,
            # e.g. "Set IP to 192.168.1.2", as the fix_id, which KeyError'd
            # in FIX_ACTIONS[...] so the restore silently never ran.)
            restore_result = run_fix("set_static_ip", ctx=self.ctx)
            # Surface a failed restore instead of silently no-op'ing — e.g.
            # the disconnected-adapter guard refuses with "cable unplugged",
            # or netsh errors. Without this the user clicks "Restore previous"
            # and nothing visibly happens.
            if not restore_result.ok:
                self.ctx.logger.warning(
                    "External IP restore failed: rc=%s %s",
                    restore_result.returncode,
                    (restore_result.stderr or "").strip(),
                )
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("Restore previous IP — failed")
                box.setText(
                    "Couldn't restore the previous IP:\n\n"
                    + _summarize_fix_error(restore_result)
                )
                if _fix_details_useful(restore_result):
                    box.setDetailedText(
                        f"Return code: {restore_result.returncode}\n"
                        f"--- stdout ---\n{restore_result.stdout or '(empty)'}\n"
                        f"--- stderr ---\n{restore_result.stderr or '(empty)'}"
                    )
                box.exec()
        else:
            self._external_change_cooldown = now + 30.0

    def _refresh_role_warning_banner(self) -> None:
        """Re-run the role/IP consistency check and update the banner."""
        rs = self.ctx.run_state
        cfg = self.ctx.config.network
        if rs.role is Role.LOOPBACK or rs.role is Role.UNDECIDED:
            rs.role_warning_text = ""
            self._role_warning_banner.setVisible(False)
            return
        warning = evaluate_role_ip_warning(
            role=rs.role,
            bound_ips=local_ipv4_addresses(),
            server_ip=str(cfg.server_ip),
            client_ip=str(cfg.client_ip),
        )
        rs.role_warning_text = warning
        if warning:
            self._role_warning_banner.setText(warning)
            self._role_warning_banner.setVisible(True)
        else:
            self._role_warning_banner.setVisible(False)
        # Notify the main window so the persistent top banner stays in
        # sync. The user dismissing the top banner doesn't clear the
        # full Setup tab banner — they have independent lifecycles.
        # (Task A, 2026-05-12.)
        window = self.window()
        if hasattr(window, "refresh_warning_banner"):
            window.refresh_warning_banner()

    def _summarise(self, results: list[CheckResult]) -> str:
        counts = {s: 0 for s in Status}
        for r in results:
            counts[r.status] += 1
        parts = []
        if counts[Status.PASS]:
            parts.append(f"{counts[Status.PASS]} pass")
        if counts[Status.WARN]:
            parts.append(f"{counts[Status.WARN]} warn")
        if counts[Status.FAIL]:
            parts.append(f"{counts[Status.FAIL]} fail")
        if counts[Status.SKIP]:
            parts.append(f"{counts[Status.SKIP]} skipped")
        return ", ".join(parts) if parts else "(no checks)"

    def _render_results(self, results: list[CheckResult]) -> None:
        self._table.setRowCount(len(results))
        for row, result in enumerate(results):
            colour, label = _STATUS_COLOURS[result.status]

            self._table.setItem(row, 0, QTableWidgetItem(""))
            self._table.setCellWidget(row, 0, _status_pill(colour, label))

            self._table.setItem(row, 1, QTableWidgetItem(result.name))

            detail_item = QTableWidgetItem(result.detail)
            detail_item.setToolTip(result.detail)
            self._table.setItem(row, 2, detail_item)

            if result.fix_action_id and result.fix_action_id in FIX_ACTIONS:
                action = FIX_ACTIONS[result.fix_action_id]
                button = QPushButton(action.label)
                # Round-21 (AAA): compact padding so the Action button hugs
                # its label instead of ballooning the column — frees width
                # for the one-line Detail column to its left.
                button.setProperty("compact", True)
                button.clicked.connect(
                    lambda _checked=False, fid=result.fix_action_id: self._on_fix(fid)
                )
                if action.needs_admin and not is_admin():
                    button.setEnabled(False)
                    button.setToolTip("Requires Administrator. Restart elevated first.")
                # Centre the button in a holder so it keeps its natural height
                # instead of stretching to fill a momentarily-tall row.
                self._table.setCellWidget(row, 3, _centered(button))
            else:
                self._table.removeCellWidget(row, 3)
                self._table.setItem(row, 3, QTableWidgetItem(""))

        self._table.resizeRowsToContents()

    def _fixable_results(self, results: list[CheckResult]) -> list[CheckResult]:
        """Subset of ``results`` whose row has a real Fix action.

        Excludes ``restart_as_admin`` — that one launches an elevated
        copy via UAC and closes the current process, which would derail
        any subsequent Fix-all step.
        """
        return [
            r for r in results
            if r.fix_action_id
            and r.fix_action_id in FIX_ACTIONS
            and r.fix_action_id != "restart_as_admin"
        ]

    def _on_fix_all(self) -> None:
        """Run every visible Fix action in sequence and report a summary."""
        results = getattr(self, "_last_results", None) or []
        fixable = self._fixable_results(results)
        if not fixable:
            QMessageBox.information(
                self, "Fix all",
                "Nothing to fix — every check already passes.",
            )
            return

        # Resolve labels up-front so the confirm dialog matches what the
        # individual Fix buttons would have shown, then ORDER the fixes so
        # subnet-clearing (disable Wi-Fi) runs before the static-IP set — see
        # order_fix_ids / _FIX_ALL_ORDER for why (prevents the APIPA fallback
        # that left the IP "configured but not bound" + unpingable).
        planned: list[tuple[str, str, str]] = []  # (fix_id, label, check_name)
        for r in fixable:
            fid = r.fix_action_id  # type: ignore[assignment]
            if fid == "set_static_ip":
                label = resolve_set_static_ip(self.ctx).label
            elif fid == "disable_wifi":
                label = resolve_disable_wifi(self.ctx).label
            else:
                label = FIX_ACTIONS[fid].label
            planned.append((fid, label, r.name))
        planned.sort(
            key=lambda t: _FIX_ALL_ORDER.get(t[0], _FIX_ALL_DEFAULT_ORDER)
        )
        plan: list[tuple[str, str]] = [(fid, label) for fid, label, _ in planned]

        items_md = "\n".join(
            f"  - {label}  ({name})" for _fid, label, name in planned
        )
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle("Run all fixes?")
        confirm.setText(f"Run {len(plan)} fix(es) in sequence?")
        confirm.setInformativeText(
            f"The following fixes will be applied:\n\n{items_md}\n\n"
            "Each runs in order. A failure won't stop the rest — a "
            "summary appears at the end."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        self._fix_all_btn.setEnabled(False)
        self._recheck_btn.setEnabled(False)
        self._status_summary.setText("Running fixes...")
        # Sub-second netsh calls don't need a QThread — processEvents()
        # between them keeps the UI breathing without the worker plumbing.
        from PySide6.QtWidgets import QApplication as _QApp

        transcript_parts: list[str] = []
        succeeded = 0
        failed = 0
        for (fid, label) in plan:
            action = FIX_ACTIONS[fid]
            if action.needs_admin and not is_admin():
                transcript_parts.append(
                    f"[skip] {label}: requires Administrator."
                )
                failed += 1
                continue
            try:
                result = run_fix(fid, ctx=self.ctx)
            except Exception as exc:  # noqa: BLE001
                transcript_parts.append(f"[ERROR] {label}: {exc}")
                failed += 1
                continue
            tag = "OK" if result.ok else "FAIL"
            transcript_parts.append(
                f"[{tag}] {label}  (rc={result.returncode})"
            )
            if not result.ok:
                # The most-useful line (skips the run_fix transcript headers),
                # not just stderr's first line — so a netsh failure shows the
                # real error, and the cable refusal shows its message.
                transcript_parts.append(
                    "        " + _summarize_fix_error(result)[:200]
                )
            if result.ok:
                succeeded += 1
            else:
                failed += 1
            _QApp.processEvents()  # keep the UI breathing

        # Persist the Wi-Fi marker if Fix-all disabled Wi-Fi (crash-recovery).
        self._persist_wifi_offline_marker()

        box = QMessageBox(self)
        box.setIcon(
            QMessageBox.Icon.Information if failed == 0
            else QMessageBox.Icon.Warning
        )
        box.setWindowTitle("Fix all — done")
        if failed == 0:
            box.setText(f"All {succeeded} fix(es) succeeded.")
        elif succeeded == 0:
            box.setText(f"All {failed} fix(es) failed — see the details.")
        else:
            box.setText(
                f"{succeeded} succeeded, {failed} failed — see the details."
            )
        box.setDetailedText("\n".join(transcript_parts))
        widen_detailed_box(box)
        box.exec()

        self.ctx.logger.info(
            "Fix-all: %d succeeded, %d failed (of %d)",
            succeeded, failed, len(plan),
        )

        self._refresh()

    def _on_fix(self, fix_id: str) -> None:
        action = FIX_ACTIONS[fix_id]

        # Dynamic fixes resolve their argv + label + message from the
        # current AppContext (role, config, detected adapter). Static
        # fixes (firewall rules, restart-as-admin) just use the FixAction
        # straight from the registry.
        if fix_id == "set_static_ip":
            resolved = resolve_set_static_ip(self.ctx)
            label = resolved.label
            argv = resolved.argv
            message = resolved.confirm_message
        elif fix_id == "disable_wifi":
            resolved = resolve_disable_wifi(self.ctx)
            label = resolved.label
            argv = resolved.argv
            message = resolved.confirm_message
        else:
            # Static fixes (firewall rules, restart-as-admin) — the
            # registry entry already carries everything we need.
            label = action.label
            argv = action.argv
            message = action.confirm_message

        # Build a readable preview of the exact command that will run.
        # Tokens containing spaces (e.g. name="Wi-Fi 2") get quoted so
        # the preview can be pasted back into a shell verbatim.
        cmd_preview = " ".join(
            f'"{tok}"' if " " in tok else tok for tok in argv
        )

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle(f"Confirm: {label}")
        confirm.setText(message)
        confirm.setDetailedText(f"Command:\n{cmd_preview}")
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        widen_detailed_box(confirm)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        # Admin guard — the button is already disabled in _render_results
        # when admin is required and we aren't elevated, but a fix can
        # still be triggered programmatically (or the elevation state
        # may have changed mid-session). Surface it as a normal failure
        # so _show_fix_outcome renders the same way as any other.
        if action.needs_admin and not is_admin():
            result = FixResult(
                ok=False,
                stdout="",
                stderr="Requires Administrator. Restart elevated first.",
                returncode=-1,
            )
        else:
            result = run_fix(fix_id, ctx=self.ctx)

        self._persist_wifi_offline_marker()
        self._show_fix_outcome(label, result)

    def _persist_wifi_offline_marker(self) -> None:
        """Save the adapter the "Disconnect Wi-Fi" fix took offline (or clear it).

        Written right after a fix so a crash / force-kill can't lose it — the
        next launch re-enables that adapter (crash-recovery) if a normal close
        didn't. Harmless for non-Wi-Fi fixes (the marker is None then).
        """
        settings.save_wifi_offline_adapter(
            self.ctx.run_state.wifi_offline_adapter
        )

    def _show_fix_outcome(self, label: str, result: FixResult) -> None:
        """Render the result of a fix to the user, then re-run checks.

        Success path shows an Information dialog and triggers a fresh
        prereq sweep so the failing row flips green without the user
        having to click Re-check. Failure path shows a Critical dialog
        whose **main text carries the actual reason** (the most-useful
        netsh/system line — cable unplugged, elevation required, a netsh
        error, …) so the user sees *why* without clicking "Details". The
        full rc+stdout+stderr transcript is attached as Details only when
        it adds something beyond that one line (so a single-line failure
        like the cable refusal — or a clean success — gets no redundant
        "Show Details" button).
        """
        box = QMessageBox(self)
        if result.ok:
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle(f"{label} - done")
            box.setText(
                "The fix completed successfully. Re-running checks..."
            )
        else:
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle(f"{label} - failed")
            box.setText(
                f"The fix did not complete:\n\n{_summarize_fix_error(result)}"
            )
        if _fix_details_useful(result):
            box.setDetailedText(
                f"Return code: {result.returncode}\n"
                f"--- stdout ---\n{result.stdout or '(empty)'}\n"
                f"--- stderr ---\n{result.stderr or '(empty)'}"
            )
            widen_detailed_box(box)
        box.exec()

        self.ctx.logger.info(
            "Fix %r %s (rc=%s)",
            label,
            "succeeded" if result.ok else "failed",
            result.returncode,
        )

        # Re-run the prereq checks regardless of outcome — even a failed
        # fix can change state (e.g. a partial netsh run) and the user
        # needs to see the current picture, not a stale snapshot.
        self._refresh()
