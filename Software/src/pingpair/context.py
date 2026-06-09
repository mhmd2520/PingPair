"""Application context - passed into views and core modules.

Eliminates global state and makes everything trivially mockable in tests.
The :class:`AppContext` carries an immutable :class:`AppConfig` (defaults
loaded from JSON) and a mutable :class:`RunState` that the GUI tabs read
and write to share the currently-selected single-case parameters.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from dataclasses import dataclass, field
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Literal

from .config import AppConfig
from .paths import REPORTS_DIR, log_dir


class Role(str, Enum):
    """Which side of a paired test this PingPair instance is playing."""

    UNDECIDED = "undecided"
    SERVER = "server"     # Laptop A: 192.168.1.1, listens, obeys
    CLIENT = "client"     # Laptop B: 192.168.1.2, drives the sweep
    LOOPBACK = "loopback"  # single-machine dev mode


@dataclass(slots=True)
class NicOverride:
    """Per-PC override of the profile's role-default NIC configuration.

    Group F (Q1, 2026-05-16): the Setup tab now offers a master checkbox
    "Use a custom IP configuration" that, when ticked, enables three input
    fields (IP / Subnet / Gateway). Values entered here override the
    profile-level defaults from :class:`config.NetworkConfig` for THIS
    machine only — both PCs can still share the same profile while each
    overrides its local NIC binding independently. Stored per-PC in
    QSettings, never in the shared profile.

    Resolution (single source of truth =
    :func:`core.nic_resolve.effective_nic_for_role`):

    * If ``use_custom`` is True AND the relevant field is non-empty
      AND the role is Server or Client -> use the override field.
    * Otherwise -> fall back to ``cfg.network.<role>_ip`` /
      ``subnet_mask`` / ``gateway`` from the profile.

    Loopback role ignores the override entirely (dev mode uses 127.0.0.1).

    All three fields are stored as strings rather than typed addresses so
    the user's in-progress (possibly malformed) input persists across
    debounce ticks without coercion errors; validation happens at apply
    time via :class:`pydantic.IPvAnyAddress` parsing.
    """

    use_custom: bool = False
    ip: str | None = None         # None / "" = use profile default
    subnet: str | None = None     # None / "" = use profile default
    gateway: str | None = None    # None / "" = no gateway (or profile default)


@dataclass(slots=True)
class RunState:
    """Mutable, GUI-shared selection for the next single-case run.

    Defaults pull from AppConfig the first time RunState is built; the
    Config tab writes here when the user changes a dropdown, and the
    Run tab reads when the Run button is clicked.
    """

    payload_bytes: int = 200
    bandwidth_mbps: int = 10
    duration_s: int = 30
    protocol: Literal["udp", "tcp"] = "udp"
    loopback: bool = False
    role: Role = Role.UNDECIDED
    server_host_override: str | None = None  # Client uses this to find the Server

    # Populated by ``app.launch_gui`` when something is off about the
    # role/IP pairing - either first-launch auto-detect didn't get a
    # clean hit, or a re-launch with a saved role found that the
    # canonical IP for that role isn't bound any more. Empty string
    # means "no warning"; the Setup tab renders it as an orange banner.
    # The Setup tab also re-runs the consistency check on every prereq
    # refresh so the banner clears automatically once the IP is fixed.
    # Cleared by the Setup tab's role switcher when the user applies
    # a new role. Not persisted.
    role_warning_text: str = ""

    # Captured when a sweep errors out (connection refused, Server
    # bind failed, etc.). Shown by the persistent top banner exactly
    # like role_warning_text — both feed into MainWindow.refresh_warning_banner.
    # Cleared at the start of every new sweep. (Task T, 2026-05-13.)
    connection_warning_text: str = ""

    # Set True by the Run tab while a sweep is in flight. The Setup
    # tab's role switcher reads this to refuse a mid-sweep role change.
    # Not persisted - derived state from the active QThread worker.
    sweep_active: bool = False

    # Set to the adapter name when the "Disconnect Wi-Fi" fix disables Wi-Fi
    # this session. In-memory only — a normal app close re-enables that
    # adapter (see MainWindow.closeEvent) so the user gets their internet
    # back; Reset restores it independently. None = Wi-Fi wasn't touched.
    wifi_offline_adapter: str | None = None

    # Group B: Client-side case-subset picker.
    # Empty list means "run all 20" (the canonical full sweep). When non-
    # empty, only the listed (1-based) case indexes get sent to the Server
    # over the control channel. Persisted via QSettings under
    # ``script/selected_case_indexes`` so the user's pruning survives an
    # app restart - matches the hands-free principle.
    selected_case_indexes: list[int] = field(default_factory=list)

    # Group C-1: continuous multi-segment mode.
    # When True, the Client panel runs sweeps in sequence with a
    # between-segments dialog instead of finishing after a single sweep.
    # Matches the train workflow where one operator walks the full train
    # hitting every car-pair. Persisted via QSettings under
    # ``script/continuous_mode``. Defaults to False so existing users
    # see no behaviour change unless they opt in.
    continuous_mode: bool = False

    # ----- Phase 4: cross-tab report state -----
    # The most recent finished SweepResult (typed at runtime as
    # pingpair.core.control.client.SweepResult). Imported lazily to avoid a
    # cycle between context.py and the control module.
    last_sweep_result: object | None = None

    # Report destination + filename pattern + selected formats.
    report_dir: Path = field(default_factory=lambda: REPORTS_DIR)
    report_filename_pattern: str = "PingPair_{date}_{time}"
    report_formats: list[str] = field(
        default_factory=lambda: ["docx", "xlsx"]
    )
    # Auto-save default flipped 2026-05-11: new users get the
    # prompt-first save flow (post-test "Save report?" dialog) and can
    # opt into hands-free auto-save via the dialog's
    # "Don't ask me in the future" checkbox. Existing users keep
    # whatever they previously persisted to QSettings — their on-disk
    # 'report/auto_save' overrides this default at app start.
    report_auto_save: bool = False
    # Whether to also write a Charts/ subfolder of PNG chart files
    # alongside each saved report. Default ON; the matplotlib renderer
    # is headless so this works in a PyInstaller bundle. (Task N,
    # 2026-05-12.)
    report_include_chart_pngs: bool = True
    # Tail of recently-saved report file paths (most recent first).
    recent_reports: list[Path] = field(default_factory=list)

    # Test-record metadata (Technician name, customer, hardware S/N, etc.).
    # Populated from QSettings by pingpair.settings; flows into RunReport
    # so it ends up on the docx title page, the xlsx Run-info sheet, etc.
    report_metadata: dict[str, str] = field(default_factory=dict)

    # Optional physical cable length under test, in metres, as the user typed
    # it (e.g. "12.50"); "" = not provided. Entered on the Run tab, persisted
    # to QSettings, and rendered into every report's metadata block. Kept as a
    # dedicated field (not in report_metadata) so editing Save-Options metadata
    # can't wipe it and it never shows as an editable Save-Options row.
    cable_length_m: str = ""

    # ----- Group F (Q1, 2026-05-16): per-PC NIC override + tracking -----
    # Optional per-machine override of the profile's role-default IP /
    # subnet / gateway. When ``nic_override.use_custom`` is True AND a
    # field is non-empty, that value beats the profile's canonical
    # ``cfg.network.<role>_ip`` for THIS PC. Persisted to QSettings under
    # ``setup/nic_override/*`` so the override survives an app restart.
    # Resolution is done by :func:`core.nic_resolve.effective_nic_for_role`
    # — the single source of truth used by check_nic_ip, the netsh fix,
    # the banner text, and the external-IP-change detection dialog.
    nic_override: NicOverride = field(default_factory=lambda: NicOverride())

    # Last IP successfully applied (either by the user's Setup tab netsh
    # fix or by accepting an external change in the detection dialog).
    # Keyed by role so each role remembers its own last-applied baseline.
    # On every prereq pass we compare the currently-bound IPs against
    # this map; a divergence triggers the "External IP change detected"
    # dialog (Keep new / Restore previous). Persisted via QSettings under
    # ``setup/last_applied_ip/<role>``.
    last_applied_ip: dict[Role, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> RunState:
        from .settings import _default_metadata
        plan = cfg.test_plan
        report_dir = cfg.report.default_dir or REPORTS_DIR
        return cls(
            payload_bytes=plan.payloads_bytes[0] if plan.payloads_bytes else 200,
            bandwidth_mbps=plan.bandwidths_mbps[0] if plan.bandwidths_mbps else 10,
            duration_s=plan.duration_s,
            protocol=plan.protocol,
            loopback=False,
            role=Role.UNDECIDED,
            server_host_override=None,
            report_dir=report_dir,
            report_filename_pattern=cfg.report.filename_pattern,
            report_formats=list(cfg.report.formats) or ["docx", "xlsx", "pdf", "txt"],
            # Prompt-first save flow: default off, user can opt into
            # auto-save via the post-test dialog's
            # "Don't ask me in the future" checkbox.
            report_auto_save=False,
            report_include_chart_pngs=True,
            report_metadata=_default_metadata(),
        )


@dataclass
class AppContext:
    """Carries config + logger + run state to anywhere in the app that needs them."""

    config: AppConfig
    logger: logging.Logger
    run_state: RunState = field(default_factory=lambda: None)  # set in create()

    # Lightweight cross-view notification for save-settings changes
    # (Group C-1 follow-up 2026-05-11). The post-test SaveReportDialog
    # mutates ``run_state.report_dir`` / ``report_filename_pattern`` /
    # ``report_auto_save`` and needs the Save Options tab's widgets to
    # follow. Rather than reach across the QTabWidget directly, the
    # script panel just calls :meth:`notify_save_settings_changed` and
    # each listener (registered by the Save Options tab during construction)
    # pulls the latest values into its widgets. Plain callables —
    # no Qt signals needed.
    save_settings_listeners: list[Callable[[], None]] = field(default_factory=list)

    # Config-changed listeners — same pattern as save_settings_listeners
    # but fires after :func:`pingpair.config.config_io.apply_config`
    # mutates ``ctx.config``.  MainWindow registers one listener that
    # rebuilds the Run tab (so the 4×5 sweep grid reflects the
    # imported test plan) and refreshes the Setup tab (so the prereq
    # rows re-evaluate against the new IPs).  Plain callables, no Qt
    # signals — listeners run synchronously on the GUI thread because
    # apply_config is only called from a button click.
    config_changed_listeners: list[Callable[[], None]] = field(default_factory=list)

    # Navigation hook wired by :class:`pingpair.app.MainWindow`: jump to a
    # Help-tab section by its cross-link key (e.g. ``"troubleshooting"``).
    # Lets any view turn an error popup or warning banner into a one-click
    # route to the relevant guide. ``None`` until the main window sets it,
    # so headless code / tests stay fully decoupled from the GUI.
    open_help: Callable[[str], None] | None = None

    @classmethod
    def create(cls, cfg: AppConfig, *, loopback: bool = False) -> AppContext:
        """Standard factory: build context + run_state from a loaded AppConfig.

        ``loopback`` is the CLI ``--loopback`` flag (True forces 127.0.0.1
        dev mode); the resulting :class:`RunState`'s ``loopback`` flag
        carries that value forward to the Run tab.
        """
        rs = RunState.from_config(cfg)
        rs.loopback = loopback
        return cls(
            config=cfg,
            logger=_build_logger(),
            run_state=rs,
        )

    def notify_save_settings_changed(self) -> None:
        """Fire every registered save-settings listener.

        Used by the post-test save dialog after the user edits the
        destination / filename pattern / auto-save flag so the Report
        tab's widgets pick up the new values without a tab switch.
        Listeners run synchronously on the GUI thread; exceptions are
        logged but don't stop the chain.
        """
        for cb in list(self.save_settings_listeners):
            try:
                cb()
            except Exception:
                self.logger.exception("save-settings listener raised; continuing")

    def notify_config_changed(self) -> None:
        """Fire every registered config-changed listener.

        Called by :func:`pingpair.config.config_io.apply_config` after
        the live :class:`AppConfig` has been mutated in place from an
        imported profile. MainWindow's listener rebuilds the Run tab
        and refreshes the Setup tab so widgets pick up the new IPs /
        ports / test grid.
        """
        for cb in list(self.config_changed_listeners):
            try:
                cb()
            except Exception:
                self.logger.exception("config-changed listener raised; continuing")


def _build_logger() -> logging.Logger:
    """Build the application's root logger.

    File handler under ``log_dir()`` with a rotating-size policy; the
    GUI also attaches a stream handler so launch lines show up in
    cmd. INFO level by default; raised to DEBUG by ``--debug``.
    """
    logger = logging.getLogger("pingpair")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        log_path = log_dir() / "pingpair.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        # Best-effort: if the log dir isn't writable for some reason
        # we still want the app to launch, just without a file log.
        pass
    # Console handler — skipped when stderr is unavailable. A frozen,
    # windowed PyInstaller build has ``sys.stderr is None``; a
    # StreamHandler over it would fail on every emit.
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger
