"""Host network-state capture for the Setup tab factory reset.

PingPair's Setup tab fixes change three things on the host: the
Ethernet NIC's IPv4 configuration ("Set the correct IP"), the Wi-Fi
adapter's admin state ("Disable Wi-Fi"), and four inbound firewall
rules. This module captures those three from the live host
(:func:`capture_snapshot`) and computes the netsh commands the
**Reset all settings** button runs to leave the host the way a fresh
install would find it (:func:`compute_factory_reset_items`).

It deliberately captures ONLY what PingPair itself touches, never the
wider host network stack. Anything netsh output can't be parsed
reliably is recorded as ``None`` ("unknown") and handled
conservatively rather than guessed at.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass, field

from .fix_actions import (
    build_release_dhcp_argv,
    detect_primary_adapter,
    detect_wifi_adapter,
)
from .prereq import FIREWALL_RULE_NAMES, _WIFI_NAME_PREFIXES
from .winexec import harden_argv

# Windows: suppress the console window each netsh probe would flash in a
# frozen GUI build. 0 is a harmless no-op elsewhere.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NetworkSnapshot:
    """The slice of host network state PingPair may change.

    Any field that could not be determined is ``None``;
    :func:`compute_factory_reset_items` handles unknowns conservatively
    (idempotent netsh re-apply for the adapters, and firewall rules
    touched only when confirmed present) rather than acting on a guess.
    """

    ethernet_adapter: str | None
    ethernet_dhcp: bool | None      # True = DHCP, False = static, None = unknown
    ethernet_ip: str | None
    ethernet_mask: str | None
    ethernet_gateway: str | None
    wifi_adapter: str | None
    # True = currently connected (has a real IPv4); False = disconnected;
    # None = no Wi-Fi adapter / couldn't determine.
    wifi_connected: bool | None
    # True = the adapter is admin-DISABLED (the test's fallback path disabled
    # it). netsh sees this even though psutil can't. None = unknown / enabled.
    wifi_admin_disabled: bool | None = None
    # First saved WLAN profile name — used to reconnect on factory reset.
    wifi_profile: str | None = None
    firewall_preexisting: dict[str, bool | None] = field(default_factory=dict)


@dataclass(slots=True)
class RestoreItem:
    """One revertible change, listed in the Setup tab Reset dialog."""

    kind: str                       # "ethernet" | "wifi" | "firewall"
    label: str
    detail: str
    commands: list[list[str]]       # netsh argv lists, run in order


# ---------------------------------------------------------------------------
# Low-level command runner
# ---------------------------------------------------------------------------


def _run(argv: list[str], timeout: float = 12.0) -> tuple[int | None, str]:
    """Run a netsh command. Returns ``(returncode | None, combined output)``.

    ``returncode`` is ``None`` when the process could not be spawned at
    all (non-Windows, OSError, timeout).
    """
    if sys.platform != "win32":
        return None, ""
    try:
        proc = subprocess.run(
            # Absolute System32 path for netsh (elevated — see winexec).
            harden_argv(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------------------
# Pure parsers (unit-tested against canned netsh output)
# ---------------------------------------------------------------------------


def _parse_ethernet_config(out: str) -> tuple[bool | None, str | None]:
    """Parse ``netsh interface ipv4 show config`` → ``(dhcp, gateway)``.

    Both come back ``None`` when the (locale-sensitive) output can't be
    matched, leaving the caller to decide how to treat an unknown
    rather than acting on a mis-parse.
    """
    dhcp: bool | None = None
    gateway: str | None = None
    m = re.search(r"DHCP\s+enabled[^\n]*?\b(Yes|No)\b", out, re.IGNORECASE)
    if m:
        dhcp = m.group(1).lower() == "yes"
    gm = re.search(
        r"Default\s+Gateway[^\n]*?(\d{1,3}(?:\.\d{1,3}){3})", out, re.IGNORECASE
    )
    if gm:
        gateway = gm.group(1)
    return dhcp, gateway


def _parse_wifi_interface(out: str) -> tuple[str | None, bool | None]:
    """Parse ``netsh interface show interface`` → ``(wifi_name, admin_disabled)``.

    netsh lists **disabled** adapters too (``psutil.net_if_addrs`` does not),
    which is how the factory reset finds a Wi-Fi NIC the test disabled and
    re-enables it. Columns: Admin State | State | Type | Interface Name.
    Returns ``(None, None)`` when no Wi-Fi-named adapter is present.
    """
    for line in out.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        admin, _state, _type, name = parts
        name = name.strip()
        if any(name.startswith(p) for p in _WIFI_NAME_PREFIXES):
            a = admin.strip().lower()
            disabled = True if a == "disabled" else (False if a == "enabled" else None)
            return name, disabled
    return None, None


def _parse_first_wlan_profile(out: str) -> str | None:
    """Parse ``netsh wlan show profiles`` → the first saved profile name.

    Lines look like ``    All User Profile     : HomeNet``. We return the
    first profile so the factory reset can reconnect Wi-Fi after a test
    disconnected it. ``None`` when no profile line is present (English
    label; non-English locales fall through to None, handled
    conservatively by the caller).
    """
    for line in out.splitlines():
        m = re.search(r"Profile\s*:\s*(.+?)\s*$", line)
        if m:
            name = m.group(1).strip()
            if name and name not in ("<None>",):
                return name
    return None


def _parse_firewall_dump(out: str, rule_name: str) -> bool | None:
    """Whether ``rule_name`` appears in a ``show rule name=all`` dump.

    ``None`` when the dump itself looks unreadable (empty / locale we
    can't recognise). Matches a whole ``Rule Name:`` line — a plain
    substring test would false-positive on a longer customer rule
    whose name merely contains one of ours.
    """
    if not out:
        return None
    if not re.search(r"^\s*(Rule Name|Enabled)\s*:", out, re.MULTILINE):
        return None
    pattern = rf"^\s*Rule Name\s*:\s*{re.escape(rule_name)}\s*$"
    return re.search(pattern, out, re.MULTILINE) is not None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _psutil_ipv4(adapter: str) -> tuple[str | None, str | None]:
    """Return ``(ip, netmask)`` for ``adapter`` via psutil — locale-free."""
    try:
        import psutil
    except ImportError:
        return None, None
    try:
        addrs = psutil.net_if_addrs().get(adapter, [])
    except Exception:  # noqa: BLE001 - psutil can raise OS-specific errors
        return None, None
    for a in addrs:
        fam = getattr(a.family, "name", "")
        is_v4 = fam in ("AF_INET", "AddressFamily.AF_INET") or (
            int(getattr(a.family, "value", -1)) == 2
        )
        if is_v4 and a.address and not a.address.startswith("127."):
            return a.address, getattr(a, "netmask", None)
    return None, None


def capture_snapshot() -> NetworkSnapshot:
    """Snapshot the host network state PingPair may change.

    Never raises — every probe is wrapped so a netsh hiccup can't break
    the Reset flow that calls this. Unreadable values become ``None``.
    """
    eth = eth_ip = eth_mask = eth_gw = None
    eth_dhcp: bool | None = None
    wifi = None
    wifi_connected: bool | None = None
    wifi_admin_disabled: bool | None = None
    wifi_profile: str | None = None
    firewall: dict[str, bool | None] = {}

    try:
        eth = detect_primary_adapter()
        if eth:
            eth_ip, eth_mask = _psutil_ipv4(eth)
            _, out = _run(
                ["netsh", "interface", "ipv4", "show", "config", f"name={eth}"]
            )
            eth_dhcp, eth_gw = _parse_ethernet_config(out)
    except Exception:  # noqa: BLE001 - capture must never break launch
        pass

    try:
        # netsh lists disabled adapters (psutil doesn't), so use it to find
        # the Wi-Fi NIC + its admin state — that's how reset can re-enable a
        # Wi-Fi the test disabled. Fall back to the psutil name if needed.
        _, iface_out = _run(["netsh", "interface", "show", "interface"])
        wifi, wifi_admin_disabled = _parse_wifi_interface(iface_out)
        if not wifi:
            wifi = detect_wifi_adapter()
        if wifi:
            # Connected = the Wi-Fi NIC currently carries a real (non-APIPA)
            # IPv4. After PingPair's "Disconnect Wi-Fi" fix runs, it won't —
            # which (together with the admin state) tells reset to restore it.
            wifi_ip, _ = _psutil_ipv4(wifi)
            wifi_connected = bool(
                wifi_ip and not wifi_ip.startswith("169.254.")
            )
            _, prof_out = _run(["netsh", "wlan", "show", "profiles"])
            wifi_profile = _parse_first_wlan_profile(prof_out)
    except Exception:  # noqa: BLE001
        pass

    try:
        _, dump = _run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"]
        )
        for rid, rname in FIREWALL_RULE_NAMES.items():
            firewall[rid] = _parse_firewall_dump(dump, rname)
    except Exception:  # noqa: BLE001
        firewall = {rid: None for rid in FIREWALL_RULE_NAMES}

    return NetworkSnapshot(
        ethernet_adapter=eth,
        ethernet_dhcp=eth_dhcp,
        ethernet_ip=eth_ip,
        ethernet_mask=eth_mask,
        ethernet_gateway=eth_gw,
        wifi_adapter=wifi,
        wifi_connected=wifi_connected,
        wifi_admin_disabled=wifi_admin_disabled,
        wifi_profile=wifi_profile,
        firewall_preexisting=firewall,
    )


# ---------------------------------------------------------------------------
# Factory reset — diff + argv builders + executor
# ---------------------------------------------------------------------------


def _build_enable_wifi_argv(adapter: str) -> list[str]:
    """netsh argv that admin-enables a NIC by friendly name.

    Covers the fallback case where the "Disconnect Wi-Fi" fix had to disable
    a virtual "Wi-Fi" adapter (``netsh wlan disconnect`` doesn't work on
    one). Re-enabling an already-enabled adapter is a harmless no-op.
    """
    return [
        "netsh", "interface", "set", "interface",
        f"name={adapter}", "admin=enabled",
    ]


def _build_reconnect_wifi_argv(adapter: str, profile: str) -> list[str]:
    """netsh argv that reconnects a Wi-Fi NIC to a saved profile.

    Mirror of the "Disconnect Wi-Fi" fix: the test disconnects Wi-Fi with
    ``netsh wlan disconnect``; the factory reset reconnects it here with
    ``netsh wlan connect`` so Wi-Fi connectivity is restored just like
    Ethernet reverts to DHCP.
    """
    return [
        "netsh", "wlan", "connect",
        f"name={profile}",
        f"interface={adapter}",
    ]


def build_delete_firewall_argv(rule_name: str) -> list[str]:
    """netsh argv that deletes a firewall rule by name.

    Deletes every rule with this exact name — safe here because the
    four ``PingPair ...`` rule names are unique to this app.
    """
    return [
        "netsh", "advfirewall", "firewall", "delete", "rule",
        f"name={rule_name}",
    ]


def plan_close_ethernet_revert(
    adapter: str | None,
    current_ip: str | None,
    server_ip: str,
    subnet_mask: str,
) -> list[list[str]]:
    """netsh command(s) to revert the primary Ethernet to DHCP on app close.

    Returns ``[build_release_dhcp_argv(adapter)]`` (the *same* command "Reset
    all settings" runs for the Ethernet NIC) ONLY when ``adapter`` is bound to
    an IP inside this deployment's test subnet (``server_ip`` masked by
    ``subnet_mask`` — e.g. ``192.168.1.0/24``) — i.e. it's plainly PingPair's
    test address. Every other case returns ``[]``:

    * no adapter / no IPv4 bound,
    * an IP on a *different* subnet (a user's own unrelated static, or a
      Loopback-dev box PingPair never configured),
    * an unparseable server IP / mask.

    The gate matters because a normal close is **silent**: it must not clobber
    a config that isn't ours (CLAUDE.md §2 "never modify the host without
    explicit confirmation"). Reset stays unconditional because it is an
    explicit factory wipe; the X button is not. The user's rig (Client on
    ``192.168.1.2``) is inside the subnet, so its confirmed revert is kept.
    """
    if not adapter or not current_ip:
        return []
    try:
        net = ipaddress.ip_network(f"{server_ip}/{subnet_mask}", strict=False)
        if ipaddress.ip_address(current_ip) in net:
            return [build_release_dhcp_argv(adapter)]
    except ValueError:
        return []
    return []


def compute_factory_reset_items(current: NetworkSnapshot) -> list[RestoreItem]:
    """Items the **Reset all settings** button should run.

    Reset's model is *factory wipe*: leave the host in the same state a
    fresh Windows install + a fresh PingPair install would produce.

    * **Firewall** — delete every PingPair rule that currently exists,
      regardless of who added it or when. We own those names.
    * **Ethernet** — revert the primary adapter to **DHCP** unless we
      know it's already on DHCP.
    * **Wi-Fi** — reconnect the adapter to its saved profile when it's
      currently disconnected (i.e. the test's "Disconnect Wi-Fi" ran) and
      a profile is known.

    Adapters that aren't detected at all are skipped — we never guess
    at an adapter name. Adapters whose state we couldn't parse (DHCP /
    Wi-Fi state = ``None``) are treated as "needs revert" because the
    underlying netsh commands are idempotent (re-setting DHCP /
    re-enabling Wi-Fi when already so is harmless). Items that are
    *definitely* already at the factory default are skipped so the
    netsh transcript stays clean. Callers gate execution on admin
    themselves.
    """
    items: list[RestoreItem] = []

    if current.ethernet_adapter and current.ethernet_dhcp is not True:
        detail = (
            f"currently {current.ethernet_ip} — revert to automatic "
            "addressing (DHCP)"
            if current.ethernet_ip
            else "revert the primary Ethernet adapter to DHCP"
        )
        items.append(RestoreItem(
            kind="ethernet",
            label=f"Ethernet ({current.ethernet_adapter}) → automatic (DHCP)",
            detail=detail,
            commands=[build_release_dhcp_argv(current.ethernet_adapter)],
        ))

    # Restore Wi-Fi when it's currently off for the test (no real IPv4). The
    # "Disconnect Wi-Fi" fix either disconnects a real WLAN or, on a VM, falls
    # back to disabling a virtual "Wi-Fi" adapter — so we re-enable the
    # adapter (harmless no-op if already enabled) and, when we know a profile,
    # reconnect to it. Both bases covered without tracking which path ran.
    if current.wifi_adapter and (
        current.wifi_admin_disabled or current.wifi_connected is False
    ):
        # Re-enable the adapter (detected via netsh even when disabled);
        # Windows auto-reconnects to the saved network on its own since the
        # profile is left on auto-connect. The explicit reconnect is a
        # belt-and-suspenders nudge. The Wi-Fi analogue of Ethernet→DHCP.
        commands = [_build_enable_wifi_argv(current.wifi_adapter)]
        if current.wifi_profile:
            commands.append(
                _build_reconnect_wifi_argv(
                    current.wifi_adapter, current.wifi_profile
                )
            )
            detail = f"re-enable Wi-Fi and reconnect to {current.wifi_profile}"
        else:
            detail = "re-enable the Wi-Fi adapter"
        items.append(RestoreItem(
            kind="wifi",
            label=f"Wi-Fi ({current.wifi_adapter}) → restore",
            detail=detail,
            commands=commands,
        ))

    existing = [
        name for rid, name in FIREWALL_RULE_NAMES.items()
        if current.firewall_preexisting.get(rid) is True
    ]
    if existing:
        items.append(RestoreItem(
            kind="firewall",
            label=f"Remove {len(existing)} PingPair firewall rule(s)",
            detail="; ".join(existing),
            commands=[build_delete_firewall_argv(n) for n in existing],
        ))

    return items


def run_restore_commands(commands: list[list[str]]) -> tuple[bool, str]:
    """Run ``commands`` in order. Returns ``(all_succeeded, transcript)``."""
    all_ok = True
    chunks: list[str] = []
    for argv in commands:
        rc, out = _run(argv)
        all_ok = all_ok and rc == 0
        chunks.append(f"$ {' '.join(argv)}\n[rc={rc}]\n{out}".rstrip())
    return all_ok, "\n\n".join(chunks)
