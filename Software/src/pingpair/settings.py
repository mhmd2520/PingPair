"""User settings persistence via :class:`QSettings`.

QSettings backs onto the OS-native store automatically — registry on
Windows (``HKCU\\Software\\PingPair\\PingPair`` for the packaged build),
plist on macOS, config file on Linux — so user preferences survive app
restarts and upgrades without us managing a config file ourselves.

The packaged build and a ``python -m pingpair`` dev run use **separate**
stores (dev gets a ``-Dev`` org suffix). They previously shared one, which
let a dev run's absolute paths leak into the packaged app's settings (the
root cause of the Round-6 report-dir bug); keeping them apart removes that
whole class of cross-contamination.

We persist:

* **Role choice** (Server / Client / Loopback / Undecided) — so the role
  picker dialog only pops on first launch.
* **Server host override** for Client mode.
* **Report destination + filename pattern + format selection + auto-save**
  toggle.
* **Test-record metadata** (technician name, customer, hardware S/N, etc.).
* **Window geometry** (size + position) and the last active tab.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings

from .context import NicOverride, Role, RunState
from .paths import REPORTS_DIR
from .theme import ThemeMode

# Single global QSettings — Qt's docs say this is fine and conventional.
ORG = "PingPair"
APP = "PingPair"


def _org_for_env(frozen: bool) -> str:
    """Org name for the QSettings store, separated by environment.

    The packaged build keeps the canonical ``PingPair`` org; a dev run
    (``python -m pingpair``) gets a ``-Dev`` suffix so the two can never
    pollute each other's settings.
    """
    return ORG if frozen else f"{ORG}-Dev"


def _q() -> QSettings:
    return QSettings(_org_for_env(bool(getattr(sys, "frozen", False))), APP)


# ---------------------------------------------------------------------------
# Test-record metadata — shared dict so the Save Options tab and the writers
# both speak the same shape.  Kept on RunState so we don't sprinkle yet
# another dataclass.
# ---------------------------------------------------------------------------


METADATA_KEYS: tuple[str, ...] = (
    "technician",
    "customer",
    "hardware_sn",
    "environment",
    "record_id",
)


def _default_metadata() -> dict[str, str]:
    return {k: "" for k in METADATA_KEYS}


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_into(rs: RunState) -> None:
    """Populate a freshly-built RunState from QSettings."""
    s = _q()

    role_str = str(s.value("role/value", Role.UNDECIDED.value))
    try:
        rs.role = Role(role_str)
    except ValueError:
        rs.role = Role.UNDECIDED
    if rs.role is Role.LOOPBACK:
        rs.loopback = True

    host = s.value("role/server_host_override", "")
    rs.server_host_override = str(host) if host else None

    rdir = s.value("report/dir", "")
    if rdir:
        # A non-blank value is a folder the user deliberately chose — used
        # verbatim. The default is stored BLANK (see ``save_from``) so it
        # re-resolves to the per-environment ``REPORTS_DIR`` on load. Dev and
        # frozen now use separate stores (see ``_org_for_env``), so the old
        # leaked-default migration heuristic (Round-6 #8) is gone — a dev path
        # can no longer reach the packaged app's store.
        rs.report_dir = Path(str(rdir))

    pattern = s.value("report/filename_pattern", "")
    if pattern:
        rs.report_filename_pattern = str(pattern)

    fmts_raw = s.value("report/formats", None)
    fmts: list[str] = []
    if isinstance(fmts_raw, str) and fmts_raw:
        fmts = [f.strip() for f in fmts_raw.split(",") if f.strip()]
    elif isinstance(fmts_raw, (list, tuple)):
        fmts = [str(f) for f in fmts_raw]
    if fmts:
        rs.report_formats = fmts

    # Default False — the documented "prompt-first" save flow (2026-05-11):
    # a fresh install with no saved key should show the save dialog, not
    # silently auto-write. Matches RunState / from_config defaults.
    rs.report_auto_save = _bool(s.value("report/auto_save", False))
    rs.report_include_chart_pngs = _bool(
        s.value("report/include_chart_pngs", True)
    )

    # Test-record metadata.
    md = _default_metadata()
    for key in METADATA_KEYS:
        val = s.value(f"metadata/{key}", "")
        if val:
            md[key] = str(val)
    rs.report_metadata = md

    # Group B: case-subset picker. Stored as a comma-separated string so
    # the QSettings registry key is human-readable in regedit. Empty or
    # missing value = "all 20 cases" (the canonical full sweep).
    rs.selected_case_indexes = _parse_int_list(
        s.value("script/selected_case_indexes", "")
    )

    # Group C-1: continuous multi-segment mode toggle.
    rs.continuous_mode = _bool(s.value("script/continuous_mode", False))

    # Optional cable length under test (metres, as typed). Empty = not set.
    rs.cable_length_m = str(s.value("run/cable_length_m", "") or "")

    # Group F (Q1, 2026-05-16): per-PC NIC override.
    # Stored under ``setup/nic_override/*``. Each field is optional —
    # missing / empty values map to None (= "use profile default").
    rs.nic_override = NicOverride(
        use_custom=_bool(s.value("setup/nic_override/use_custom", False)),
        ip=(str(s.value("setup/nic_override/ip", "")) or None),
        subnet=(str(s.value("setup/nic_override/subnet", "")) or None),
        gateway=(str(s.value("setup/nic_override/gateway", "")) or None),
    )

    # Last successfully-applied IP per role, used by the external-IP-change
    # detection dialog. Keys: setup/last_applied_ip/server, .../client.
    # Loopback isn't tracked (127.0.0.1 is constant).
    last_applied: dict[Role, str] = {}
    for role in (Role.SERVER, Role.CLIENT):
        val = str(s.value(f"setup/last_applied_ip/{role.value}", "") or "")
        if val:
            last_applied[role] = val
    rs.last_applied_ip = last_applied


def save_from(rs: RunState) -> None:
    """Persist relevant RunState fields to QSettings."""
    s = _q()
    s.setValue("role/value", rs.role.value)
    s.setValue("role/server_host_override", rs.server_host_override or "")
    # Round-6 #8: store the report dir only when it differs from the computed
    # default (REPORTS_DIR — beside the .exe when frozen, Software/Reports in
    # dev). A blank value means "use this environment's default", re-resolved
    # on load. This keeps reports beside the .exe even though dev + frozen share
    # one QSettings store and the install folder can move — the absolute path of
    # one environment must never pin the other. A user-chosen custom folder
    # differs from REPORTS_DIR, so it's stored verbatim and honoured.
    s.setValue(
        "report/dir",
        "" if _same_path(rs.report_dir, REPORTS_DIR) else str(rs.report_dir),
    )
    s.setValue("report/filename_pattern", rs.report_filename_pattern)
    s.setValue("report/formats", ",".join(rs.report_formats))
    s.setValue("report/auto_save", "true" if rs.report_auto_save else "false")
    s.setValue(
        "report/include_chart_pngs",
        "true" if rs.report_include_chart_pngs else "false",
    )
    md = getattr(rs, "report_metadata", None) or _default_metadata()
    for key in METADATA_KEYS:
        s.setValue(f"metadata/{key}", md.get(key, ""))
    s.setValue(
        "script/selected_case_indexes",
        ",".join(str(i) for i in (rs.selected_case_indexes or [])),
    )
    s.setValue(
        "script/continuous_mode",
        "true" if rs.continuous_mode else "false",
    )
    s.setValue("run/cable_length_m", getattr(rs, "cable_length_m", "") or "")
    # Group F (Q1, 2026-05-16): per-PC NIC override persistence.
    ov = rs.nic_override
    s.setValue(
        "setup/nic_override/use_custom",
        "true" if ov.use_custom else "false",
    )
    s.setValue("setup/nic_override/ip", ov.ip or "")
    s.setValue("setup/nic_override/subnet", ov.subnet or "")
    s.setValue("setup/nic_override/gateway", ov.gateway or "")
    # Last-applied IP per role for external-change detection.
    for role in (Role.SERVER, Role.CLIENT):
        s.setValue(
            f"setup/last_applied_ip/{role.value}",
            rs.last_applied_ip.get(role, ""),
        )
    s.sync()


def save_window_geometry(geometry: bytes | bytearray) -> None:
    _q().setValue("window/geometry", bytes(geometry))


def load_window_geometry() -> bytes | None:
    raw = _q().value("window/geometry", None)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    return None


def save_active_tab(index: int) -> None:
    _q().setValue("window/active_tab", int(index))


def load_active_tab() -> int:
    raw = _q().value("window/active_tab", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def save_wifi_offline_adapter(name: str | None) -> None:
    """Persist (or clear) the Wi-Fi adapter PingPair disabled for a test.

    Written immediately when the "Disable Wi-Fi" fix runs so a crash /
    force-kill can't lose it; the next launch reads it and re-enables the
    adapter (crash-recovery) when a normal close didn't already. Pass
    ``None`` to clear.
    """
    s = _q()
    s.setValue("network/wifi_offline_adapter", name or "")
    s.sync()


def load_wifi_offline_adapter() -> str | None:
    """The Wi-Fi adapter a prior session disabled and may not have restored."""
    raw = _q().value("network/wifi_offline_adapter", "")
    name = str(raw) if raw else ""
    return name or None


def save_ethernet_revert_pending(name: str | None) -> None:
    """Persist (or clear) the Ethernet adapter whose on-close DHCP revert FAILED.

    The X-button close reverts the primary Ethernet to DHCP; if that netsh
    call fails (rc≠0) the NIC is stranded on the test IP. We record the
    adapter so the next launch can finish the revert (crash-recovery, mirror
    of :func:`save_wifi_offline_adapter`). Cleared the moment a revert
    succeeds. Pass ``None`` to clear.
    """
    s = _q()
    s.setValue("network/ethernet_revert_pending", name or "")
    s.sync()


def load_ethernet_revert_pending() -> str | None:
    """The Ethernet adapter a prior close failed to revert to DHCP, if any."""
    raw = _q().value("network/ethernet_revert_pending", "")
    name = str(raw) if raw else ""
    return name or None


def load_welcome_seen() -> bool:
    """Whether the first-boot welcome screen has already been shown.

    False on a fresh install (no key) so the welcome tour appears exactly
    once; set True the first time it's dismissed. Cleared by the factory
    reset (``reset_all`` wipes the whole store), so a reset re-arms it.
    """
    return _bool(_q().value("app/welcome_seen", False))


def save_welcome_seen(seen: bool) -> None:
    s = _q()
    s.setValue("app/welcome_seen", "true" if seen else "false")
    s.sync()


# ---------------------------------------------------------------------------
# Feature 6 — in-app update check. Three keys under the same ``app/``
# namespace as ``welcome_seen``; all cleared by the factory reset
# (``reset_all`` wipes the whole store), which re-arms the auto-check.
# ---------------------------------------------------------------------------


def load_updates_auto_check() -> bool:
    """Whether to check GitHub for a newer release shortly after launch.

    Defaults True on a fresh install. The About tab exposes the opt-out;
    the manual "Check for updates" button works regardless of this flag.
    """
    return _bool(_q().value("app/updates_auto_check", True), default=True)


def save_updates_auto_check(enabled: bool) -> None:
    s = _q()
    s.setValue("app/updates_auto_check", "true" if enabled else "false")
    s.sync()


def load_updates_last_check_ts() -> float:
    """Unix timestamp of the last auto-check, for the ~once/24h throttle.

    0.0 (the default) means "never checked", so the first launch always
    runs the auto-check. A garbage value coerces back to 0.0.
    """
    try:
        return float(_q().value("app/updates_last_check_ts", 0.0))
    except (TypeError, ValueError):
        return 0.0


def save_updates_last_check_ts(ts: float) -> None:
    s = _q()
    s.setValue("app/updates_last_check_ts", str(float(ts)))
    s.sync()


def load_theme() -> ThemeMode:
    """Saved appearance mode; defaults to System on a fresh install."""
    return ThemeMode.coerce(_q().value("appearance/theme", ThemeMode.SYSTEM.value))


def save_theme(mode: ThemeMode | str) -> None:
    """Persist the appearance mode (Light / Dark / System)."""
    m = mode.value if isinstance(mode, ThemeMode) else str(mode)
    s = _q()
    s.setValue("appearance/theme", m)
    s.sync()


def load_sounds_enabled() -> bool:
    """Whether to play notification sounds on sweep finish / error / prompt.

    Defaults True on a fresh install (Round-6 #7). Toggled on the Setup tab's
    Appearance box; cleared by the factory reset (``reset_all`` wipes the whole
    store), which re-arms the default."""
    return _bool(_q().value("app/sounds_enabled", True), default=True)


def save_sounds_enabled(enabled: bool) -> None:
    s = _q()
    s.setValue("app/sounds_enabled", "true" if enabled else "false")
    s.sync()


def reset_all() -> None:
    """Clear every persisted setting — the Setup tab factory reset.

    Wipes the role, report, metadata, script, setup, window, and app
    preference keys. Defaults are re-derived from the AppConfig on the
    next launch. Saved test-plan profiles in ``Configs\\`` and reports
    in ``Reports\\`` are untouched — those are files, not QSettings.
    """
    s = _q()
    s.clear()
    s.sync()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _same_path(a: Path, b: Path) -> bool:
    """True when two paths point at the same location, resolve-tolerant.

    ``Path.resolve`` (strict=False) normalises case + separators + ``..`` so
    the report-dir default comparison (Round-6 #8) survives a path typed with
    a different case or a trailing component shape. Falls back to a plain
    equality check if resolution raises (e.g. a malformed path)."""
    try:
        return a.resolve() == b.resolve()
    except (OSError, ValueError, RuntimeError):
        return a == b


def _parse_int_list(raw: Any) -> list[int]:
    """Parse a list of ints, garbage-tolerant. Three input shapes:

    * **Comma-separated string** (typical QSettings case):
      ``"1, 2 ,3,,abc,7"`` -> ``[1, 2, 3, 7]``. Whitespace stripped;
      non-integer tokens dropped.

    * **Pre-coerced list/tuple** (some Qt platforms auto-convert):
      ``[1, "2", 3.0, None, "x"]`` -> ``[1, 2, 3]``. ``int`` passes
      through; ``float`` is truncated via ``int(float)``; ``str`` is
      parsed and stripped; ``None`` / unparseable values are dropped;
      ``bool`` is dropped (avoids ``True/False`` sneaking in as 0/1
      since bool subclasses int).

    * **None / "" / other types** -> ``[]``. The test contract
      requires that scalar non-string-non-list inputs like ``42``
      do NOT coerce into a single-element list; they map to empty.

    Used by Group B's case-subset persistence (1-based case indexes).
    Order is preserved; duplicates are skipped; values ``< 1`` are
    dropped (a 0 or negative is never a valid 1-based case index — it
    only ever reaches here from a stale or hand-edited QSettings value).
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = raw.split(",")
    else:
        # Non-string non-list types (int, float, bytes, ...) yield empty
        # per the test contract. Without this guard, _parse_int_list(42)
        # would fall through to str(42).split(",") and return [42].
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in items:
        try:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                n = item
            elif isinstance(item, float):
                n = int(item)
            elif isinstance(item, str):
                token = item.strip()
                if not token:
                    continue
                n = int(token)
            else:
                continue
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue  # 1-based indexes only — see docstring
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _bool(raw: Any, *, default: bool = False) -> bool:
    """Coerce a QSettings value to bool, accepting the common shapes.

    QSettings on Windows persists booleans as the literal strings
    ``"true"`` / ``"false"`` (lowercase). On Linux + macOS Qt sometimes
    returns actual Python booleans. We accept both, plus the integer
    0/1 forms and a handful of common spellings.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in {"true", "yes", "on", "1"}:
            return True
        if s in {"false", "no", "off", "0", ""}:
            return False
    return default
