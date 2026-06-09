"""Confirm-and-fix actions for failed prereq checks.

Every entry is a self-contained recipe: a label for the button, a
plain-English confirmation message, and the exact ``netsh`` argv that
will be executed.  The view shows the confirm message and the argv (so
the user sees what's about to run) before invoking :func:`run_fix`.

Most fixes are static (firewall rules — same argv regardless of state),
but ``set_static_ip`` is dynamic: the IP to set depends on the current
:class:`~pingpair.context.Role` and the adapter name has to be detected
at click-time. The view passes the ``AppContext`` into :func:`run_fix`
and :func:`resolve_set_static_ip` so the argv can be built fresh.

Adding new fixes: register a new :class:`FixAction` in :data:`FIX_ACTIONS`
and have a ``check_*`` in :mod:`prereq` set ``fix_action_id`` to its key.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .prereq import (
    _NO_WINDOW,
    _is_ipv4,
    _WIFI_NAME_PREFIXES,
    FIREWALL_RULE_NAMES,
)
from .winexec import harden_argv

if TYPE_CHECKING:
    import socket

    from ..context import AppContext


@dataclass(frozen=True, slots=True)
class FixAction:
    """One executable remedy."""

    id: str
    label: str            # button text
    confirm_message: str  # shown before run
    argv: list[str]       # what will actually be executed
    needs_admin: bool = True


@dataclass(frozen=True, slots=True)
class FixResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _add_firewall_rule(name: str, *args: str) -> list[str]:
    return [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={name}",
        "dir=in",
        "action=allow",
        *args,
    ]


FIX_ACTIONS: dict[str, FixAction] = {
    "open_icmp": FixAction(
        id="open_icmp",
        label="Add ICMP rule",
        confirm_message=(
            "Add an inbound Windows Firewall rule that allows ICMPv4 echo "
            "requests (ping). This is what PingPair needs to use fping for "
            "latency measurement."
        ),
        argv=_add_firewall_rule(
            FIREWALL_RULE_NAMES["icmp"],
            "protocol=icmpv4:8,any",
        ),
    ),
    "open_iperf3_ports": FixAction(
        id="open_iperf3_ports",
        label="Open 5201 (TCP+UDP)",
        confirm_message=(
            "Add two inbound Windows Firewall rules that allow TCP and UDP "
            "traffic on port 5201 — the iperf3 default. PingPair's "
            "throughput tests use UDP; TCP is added for completeness."
        ),
        # Composite: two separate netsh calls. Encoded as one logical action;
        # run_fix() handles list-of-lists internally if needed via subcommands.
        argv=_add_firewall_rule(
            FIREWALL_RULE_NAMES["iperf3_tcp"],
            "protocol=TCP",
            "localport=5201",
        ),
    ),
    "open_iperf3_udp": FixAction(
        id="open_iperf3_udp",
        label="Open 5201 (UDP)",
        confirm_message="Add UDP/5201 inbound rule (paired with the TCP one).",
        argv=_add_firewall_rule(
            FIREWALL_RULE_NAMES["iperf3_udp"],
            "protocol=UDP",
            "localport=5201",
        ),
    ),
    "open_control_port": FixAction(
        id="open_control_port",
        label="Open 5202 (TCP)",
        confirm_message=(
            "Add an inbound Windows Firewall rule for TCP port 5202 — the "
            "PingPair Server↔Client control channel (Phase 3)."
        ),
        argv=_add_firewall_rule(
            FIREWALL_RULE_NAMES["control"],
            "protocol=TCP",
            "localport=5202",
        ),
    ),
    "set_static_ip": FixAction(
        id="set_static_ip",
        label="Set the correct IP",
        confirm_message=(
            "Set the static IPv4 address on this PC's primary Ethernet "
            "adapter to the canonical IP for the currently-selected role "
            "(Server → 192.168.1.1, Client → 192.168.1.2). Falls back to "
            "opening the Windows adapter settings panel if the adapter "
            "can't be auto-detected or the role is Loopback."
        ),
        # Argv is built at click-time by :func:`resolve_set_static_ip`
        # based on the current role + detected adapter; the placeholder
        # below is what gets shown if the resolver couldn't pick an IP.
        argv=["control", "ncpa.cpl"],
        needs_admin=True,
    ),
    "restart_as_admin": FixAction(
        id="restart_as_admin",
        label="Restart as Administrator",
        confirm_message=(
            "Relaunch PingPair with Administrator privileges. The current "
            "window will close."
        ),
        argv=[],  # handled specially via ShellExecuteW
        needs_admin=False,
    ),
    "disable_wifi": FixAction(
        id="disable_wifi",
        label="Disconnect Wi-Fi",
        confirm_message=(
            "Disconnect the Wi-Fi adapter on this PC from its network for the "
            "duration of the test. Without this, Windows can route 192.168.1.x "
            "traffic over Wi-Fi instead of the dedicated Ethernet link and "
            "you'll see phantom packet loss in the report.\n\n"
            "The adapter stays enabled — the driver is NOT disabled, only the "
            "connection drops. Reconnect from the Windows tray, or use "
            "Reset all settings on the Setup tab to restore Wi-Fi."
        ),
        # Argv built at click-time by :func:`resolve_disable_wifi`
        # because the actual adapter name varies (Wi-Fi / Wireless / WLAN).
        argv=["control", "ncpa.cpl"],
        needs_admin=True,
    ),
}


# ---------------------------------------------------------------------------
# Adapter detection + dynamic argv resolver for set_static_ip
# ---------------------------------------------------------------------------


# Adapter-name prefixes we never want to assign 192.168.1.x to.  Loopback
# and VMware host-only / vEthernet / Wi-Fi etc. are not the test LAN.
_ADAPTER_SKIP_PREFIXES: tuple[str, ...] = (
    "Loopback",
    "vEthernet",
    "vmnet",
    "VMware",
    "VirtualBox",
    "Wi-Fi",
    "Wireless",
    "Bluetooth",
    "isatap",
    "Teredo",
)


def detect_primary_adapter() -> str | None:
    """Return the friendly name of the primary Ethernet adapter, or None.

    Picks the highest-scoring candidate among adapters that:

    * Don't start with one of the well-known virtual / wireless prefixes
      (``Loopback``, ``vEthernet``, ``VMware``, ``Wi-Fi``, etc.).
    * Are physically present (have at least one address of any kind bound).

    Adapters that only have an APIPA (``169.254.x.x``) IPv4 still count —
    that's the exact case where the user needs the IP-fix most (DHCP
    didn't get a lease, adapter has fallback APIPA only). Previously we
    excluded those, which made the fix silently fall back to opening
    ncpa.cpl. (#13 follow-up 2026-05-12)

    Adapters with NO IPv4 at all but a real link-local IPv6 still pass
    too — they're plugged in, just not addressed yet, which again is
    exactly when the fix is needed.

    The returned name is the same string ``netsh interface ipv4 set
    address name=<...>`` accepts — no translation.
    """
    try:
        import psutil  # imported lazily so non-Windows tests don't import it
    except ImportError:
        return None

    candidates: list[tuple[int, str]] = []
    for iface, addrs in psutil.net_if_addrs().items():
        if any(iface.startswith(p) for p in _ADAPTER_SKIP_PREFIXES):
            continue
        # Must have AT LEAST one address of any kind — proxy for
        # "physically present and not disabled".
        if not addrs:
            continue
        # Score: prefer Ethernet*, then any non-virtual.
        score = 100 if iface.startswith("Ethernet") else 50
        # Bonus: adapters with a real (non-APIPA, non-loopback) IPv4
        # already up are slightly more likely to be the right one,
        # but we no longer EXCLUDE adapters without one — that was
        # the bug.
        for addr in addrs:
            ip = str(addr.address)
            if "." in ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                score += 10
                break
        candidates.append((score, iface))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (-t[0], t[1]))
    return candidates[0][1]


def adapter_link_up(adapter: str) -> bool:
    """True if ``adapter`` has a usable physical link (cable in, NIC enabled).

    Same signal :func:`prereq.check_ethernet_cable` uses to FAIL the cable
    row: ``psutil.net_if_stats()[adapter].isup`` is ``False`` when the cable
    is unplugged (no carrier) or the adapter is administratively down. Used
    to STOP :func:`run_fix` from assigning a static IP to a disconnected
    adapter — Windows silently drops the address to "media disconnected" and
    the test can't run, so configuring it is worse than useless.

    Conservative on uncertainty: if psutil is missing or the adapter isn't in
    the stats table we return ``True`` (don't block the fix on a missing
    dependency — the cable prereq still surfaces the problem separately). A
    cable-out-but-enabled NIC reads ``isup=False``; a cable-in NIC that merely
    failed DHCP (APIPA) reads ``isup=True``, so this never blocks the very
    DHCP-failed case the IP-fix exists to recover.
    """
    try:
        import psutil  # lazy import so non-Windows tests don't need it
    except ImportError:
        return True
    try:
        st = psutil.net_if_stats().get(adapter)
    except Exception:  # noqa: BLE001 - psutil can raise on odd NICs
        return True
    return True if st is None else bool(st.isup)


def adapter_for_ipv4(ip: str) -> str | None:
    """Return the adapter name currently carrying IPv4 ``ip``, or None.

    Used by the sweep's mid-case link monitor (``core.control.client``) to
    learn *which* NIC the control connection rides — its local socket
    address — so the monitor can poll that adapter's carrier
    (:func:`adapter_link_up`) and catch a cable-pull within ~1 s instead of
    waiting for the end-of-case CASE_DONE/SERVER_RESULT exchange.

    Returns ``None`` when psutil is missing, enumeration fails, or the IP
    isn't bound to any adapter (e.g. loopback, or the address was already
    yanked). The caller treats ``None`` as "don't watch the link" — so a
    failed lookup never produces a false cable-pull alarm. Never raises.
    """
    try:
        import psutil  # lazy import so non-Windows tests don't need it
    except ImportError:
        return None
    try:
        addrs_by_iface = psutil.net_if_addrs()
    except Exception:  # noqa: BLE001 - psutil can raise on odd NICs
        return None
    for iface, addrs in addrs_by_iface.items():
        for addr in addrs:
            if _is_ipv4(addr) and addr.address == ip:
                return iface
    return None


def adapter_for_socket(sock: socket.socket | None) -> str | None:
    """NIC carrying ``sock``'s local IPv4 — for the mid-sweep link watch.

    Resolves the socket's *local* address to an adapter name via
    :func:`adapter_for_ipv4`, so the sweep's link monitor can poll that
    adapter's carrier and catch a cable-pull in ~1 s. Returns ``None``
    (→ caller skips the link watch, no false alarm) for a ``None`` /
    loopback / unresolvable socket, or when psutil can't map the address.
    Best-effort; never raises. Shared by ``core.control.client`` and
    ``core.control.server`` so the resolve policy lives in one place.
    """
    if sock is None:
        return None
    try:
        local_ip = sock.getsockname()[0]
    except OSError:
        return None
    if not local_ip or local_ip.startswith("127."):
        return None
    try:
        return adapter_for_ipv4(local_ip)
    except Exception:  # noqa: BLE001 — link-watch is best-effort
        return None


def build_set_static_ip_argv(
    *,
    adapter: str,
    ip: str,
    subnet_mask: str,
    gateway: str | None = None,
) -> list[str]:
    """Build the netsh argv that assigns a static IPv4 to ``adapter``.

    Uses the explicit-keyword form (``source=static address=… mask=…``)
    rather than the positional shorthand (``static <ip> <mask>``).
    The keyword form is more forgiving when the adapter is currently
    on DHCP — the shorthand can return rc=1 with no stderr in that
    case on some Windows builds. (#13 follow-up 2026-05-12)

    Group F (Q1, 2026-05-16): optional ``gateway`` appends
    ``gateway=<gw> gwmetric=1`` to the netsh command when supplied
    (non-empty string). ``gwmetric=1`` keeps the test-NIC gateway from
    outranking the user's normal internet route. Empty / None gateway
    keeps the legacy "no gateway" point-to-point behaviour.

    Pure helper so the test suite can lock in the exact argv shape
    without spawning subprocesses.
    """
    argv = [
        "netsh", "interface", "ipv4", "set", "address",
        f"name={adapter}",
        "source=static",
        f"address={ip}",
        f"mask={subnet_mask}",
    ]
    if gateway:
        argv.append(f"gateway={gateway}")
        argv.append("gwmetric=1")
    return argv


def build_release_dhcp_argv(adapter: str) -> list[str]:
    """netsh argv that flips an adapter back to DHCP mode.

    Used as a fallback step when the static-set command fails — sometimes
    Windows needs the adapter to release any cached DHCP lease before
    accepting a static assignment.
    """
    return [
        "netsh", "interface", "ipv4", "set", "address",
        f"name={adapter}",
        "source=dhcp",
    ]


def _record_last_applied_ip(ctx: "AppContext", argv: list[str]) -> None:
    """Pull the IP out of a successfully-applied netsh argv and store it
    in ``ctx.run_state.last_applied_ip[role]``.

    Group F (Q1, 2026-05-16): seeds the baseline for the external-IP-
    change detection dialog (task #23). On every prereq pass, the
    Setup tab compares this stored value against the currently-bound
    IP — a divergence triggers the "External IP change detected"
    prompt. Without this seeding, the dialog would fire after EVERY
    apply (because last_applied_ip would still be empty).
    """
    # Late import to avoid the import cycle context → core → context.
    from ..context import Role

    role = ctx.run_state.role
    if role not in (Role.SERVER, Role.CLIENT):
        return
    for token in argv:
        if token.startswith("address="):
            ip = token[len("address="):]
            try:
                ctx.run_state.last_applied_ip[role] = ip
            except (KeyError, TypeError, AttributeError):
                # Defensive — never let a stored-state failure break the fix.
                pass
            return



@dataclass(frozen=True, slots=True)
class ResolvedFix:
    """Effective (label, argv, message) for a dynamic fix action.

    ``set_static_ip`` is the only fix that builds its argv at click-time
    today; the resolver returns this so the view can both update the
    confirm-dialog preview and run the right command.
    """

    label: str
    argv: list[str]
    confirm_message: str
    fallback: bool  # True if we couldn't build the netsh argv and fell back to ncpa.cpl


def resolve_set_static_ip(ctx: "AppContext") -> ResolvedFix:
    """Resolve the ``set_static_ip`` fix to a concrete argv at click-time.

    Behaviour matrix:

    * **Server / Client** role → ``netsh ... source=static address=<ip>
      mask=<mask>`` using the effective IP/subnet/gateway from
      :func:`core.nic_resolve.effective_nic_for_role`. The override
      (Group F / Q1, 2026-05-16) beats the profile when applied; the
      profile default applies otherwise.
    * **Loopback / Undecided** → fall back to opening ``ncpa.cpl`` since
      there's no canonical IP to assign in dev mode.
    * **No adapter detected** → also fall back to ``ncpa.cpl`` so the user
      can pick the adapter and IP themselves.
    """
    # Late import to avoid the import cycle context → core → context.
    from ..context import Role
    from .nic_resolve import effective_nic_for_role

    role = ctx.run_state.role
    override = ctx.run_state.nic_override

    if role not in (Role.SERVER, Role.CLIENT):
        return ResolvedFix(
            label="Open Network adapter settings",
            argv=["control", "ncpa.cpl"],
            confirm_message=(
                "The current role is Loopback or undecided, so there's no "
                "canonical IP to assign automatically. Opening the Windows "
                "adapter settings panel instead — set the IP manually."
            ),
            fallback=True,
        )

    # Resolve the effective config — picks override fields when applied,
    # falls back per-field to profile defaults otherwise.
    eff = effective_nic_for_role(role, ctx.config, override)
    target_ip = eff.ip
    target_mask = eff.subnet_mask
    target_gateway = eff.gateway  # None = no gateway clause
    source_tag = " (custom)" if eff.source == "override" else ""

    adapter = detect_primary_adapter()
    if not adapter:
        return ResolvedFix(
            label="Open Network adapter settings",
            argv=["control", "ncpa.cpl"],
            confirm_message=(
                "Couldn't auto-detect a primary Ethernet adapter on this PC. "
                "Opening the Windows adapter settings panel instead — pick the "
                f"correct adapter and assign {target_ip} ({target_mask})."
            ),
            fallback=True,
        )

    argv = build_set_static_ip_argv(
        adapter=adapter,
        ip=target_ip,
        subnet_mask=target_mask,
        gateway=target_gateway,
    )
    gw_msg = (
        f" with gateway {target_gateway}"
        if target_gateway
        else " (no gateway — point-to-point LAN)"
    )
    return ResolvedFix(
        label=f"Set IP to {target_ip}{source_tag}",
        argv=argv,
        confirm_message=(
            f"Assign static IPv4 {target_ip} (mask {target_mask}){gw_msg} "
            f'to adapter "{adapter}" on this PC, matching the configured '
            f"{role.value} role{source_tag}. Requires Administrator. The change "
            "is immediate — your existing connection on that adapter will "
            "drop briefly while netsh re-binds it."
        ),
        fallback=False,
    )


def detect_wifi_adapter() -> str | None:
    """Return the friendly name of the first Wi-Fi adapter, or None.

    Matches anything whose name starts with one of the well-known
    Wi-Fi name prefixes (``Wi-Fi``, ``Wireless``, ``WLAN``). The same
    prefix list is used by :func:`prereq.check_wifi_off`, so detection
    and remediation always agree on what counts as Wi-Fi.

    Returns the *first* matching adapter name. Most PCs have only one;
    on the rare multi-radio setup the user can disable additional
    adapters manually via ncpa.cpl.
    """
    try:
        import psutil
    except ImportError:
        return None

    for iface in psutil.net_if_addrs().keys():
        if any(iface.startswith(p) for p in _WIFI_NAME_PREFIXES):
            return iface
    return None


def build_disable_wifi_argv(adapter: str) -> list[str]:
    """Return the netsh argv that takes a Wi-Fi adapter offline for the test.

    Uses ``netsh interface set interface admin=disabled``, which works on
    *any* NIC on any laptop — we never hardcode an adapter; the name comes
    from :func:`detect_wifi_adapter` (the Wi-Fi / Wireless / WLAN name
    prefix). Disabling is the only reliable way to keep Wi-Fi off for the
    test: a plain ``wlan disconnect`` auto-reconnects within a second on a
    "connect automatically" profile, and on a VM a virtual "Wi-Fi" NIC
    isn't a real WLAN interface at all. The *adapter* (not the driver) goes
    down; PingPair re-enables it on a normal close and on Reset, and because
    the profile is left on auto-connect, Windows then reconnects to the saved
    network on its own.

    Pure helper so tests can lock in the argv shape without netsh.
    """
    return [
        "netsh", "interface", "set", "interface",
        f"name={adapter}",
        "admin=disabled",
    ]


def resolve_disable_wifi(_ctx: "AppContext | None" = None) -> ResolvedFix:
    """Resolve the ``disable_wifi`` fix to a concrete argv at click-time.

    Finds the Wi-Fi adapter by name (works on any laptop's Wi-Fi) and takes
    it offline. Falls back to opening ncpa.cpl when no Wi-Fi NIC is found.
    """
    adapter = detect_wifi_adapter()
    if not adapter:
        return ResolvedFix(
            label="Open Network adapter settings",
            argv=["control", "ncpa.cpl"],
            confirm_message=(
                "Couldn't find a Wi-Fi / Wireless adapter on this PC by name. "
                "Opening the Windows adapter settings panel instead — take the "
                "wireless NIC offline manually before running a sweep."
            ),
            fallback=True,
        )
    argv = build_disable_wifi_argv(adapter)
    return ResolvedFix(
        label=f'Disable "{adapter}" for the test',
        argv=argv,
        confirm_message=(
            f'Take the Wi-Fi adapter "{adapter}" offline for the test so '
            "Windows can't route PingPair's 192.168.1.x traffic over it "
            "(otherwise you'll see phantom packet loss). This disables the "
            "adapter — not the driver. PingPair re-enables it automatically "
            "when you close the app or click Reset all settings, and Windows "
            "then reconnects to your saved network on its own."
        ),
        fallback=False,
    )


def is_admin() -> bool:
    """True if the current process can modify firewall rules."""
    if sys.platform == "win32":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    import os
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _adapter_name_from_argv(argv: list[str]) -> str | None:
    """Pull the adapter name out of a ``netsh ... name=<adapter> ...`` argv."""
    for token in argv:
        if token.startswith("name="):
            return token[len("name="):]
    return None


def run_fix(fix_id: str, *, ctx: "AppContext | None" = None) -> FixResult:
    """Run the chosen fix.  Returns FixResult; never raises on subprocess error.

    Pass ``ctx`` so dynamic fixes (currently just ``set_static_ip``) can
    read the current role and config when they build their argv.
    """
    action = FIX_ACTIONS[fix_id]

    if action.id == "restart_as_admin":
        return _restart_as_admin()

    # set_static_ip resolves its argv at click-time from role + adapter.
    # Two-attempt strategy when the adapter is currently on DHCP: the
    # first 'set address ... source=static' often succeeds outright, but
    # on some Windows builds a stale DHCP lease has to be released first.
    # We try the primary argv, and on failure we release DHCP then retry
    # the static set. The combined output goes back to the caller so the
    # error dialog shows what actually happened.
    if action.id == "set_static_ip":
        if ctx is None:
            return FixResult(
                ok=False, stdout="",
                stderr="set_static_ip requires AppContext to resolve role + adapter.",
                returncode=-1,
            )
        resolved = resolve_set_static_ip(ctx)
        # Never assign a static IP to a DISCONNECTED adapter: Windows silently
        # drops the address to "media disconnected" (ipconfig shows no IP /
        # APIPA), so the config "takes" but the NIC stays unusable and the
        # test can't run. The Ethernet-cable prereq already FAILs in this
        # state; this is the single chokepoint that stops EVERY assign path
        # (the Fix button, Fix-all, and the external-IP "Restore previous")
        # from configuring it. Only guards a real resolved adapter — the
        # ncpa.cpl fallback (Loopback / no adapter) is left alone.
        if not resolved.fallback:
            adapter = _adapter_name_from_argv(resolved.argv)
            if adapter and not adapter_link_up(adapter):
                return FixResult(
                    ok=False,
                    stdout="",
                    stderr=(
                        f'Ethernet cable unplugged on "{adapter}" (media '
                        "disconnected). Connect the cable to the other PC, "
                        "then Re-check — PingPair won't assign an IP to a "
                        "disconnected adapter."
                    ),
                    returncode=-1,
                )
        first = _run_argv(resolved.argv)
        if first.ok and not resolved.fallback:
            # Seed last_applied_ip for the external-IP-change detector (Q1
            # task #23). Pulls the IP back out of the resolved argv so we
            # record exactly what netsh just applied.
            _record_last_applied_ip(ctx, resolved.argv)
        if first.ok or resolved.fallback:
            # ncpa.cpl fallback always reports ok; nothing to retry.
            return first
        # Find the adapter name from the resolved argv so we can build
        # the DHCP-release command without re-running detection.
        adapter = _adapter_name_from_argv(resolved.argv)
        if adapter is None:
            return first
        # Release DHCP, then retry static.
        release = _run_argv(build_release_dhcp_argv(adapter))
        retry = _run_argv(resolved.argv)
        combined_stdout = (
            f"--- attempt 1 (static) ---\n{first.stdout}\n"
            f"--- DHCP release ---\n{release.stdout}\n"
            f"--- attempt 2 (static after release) ---\n{retry.stdout}"
        )
        combined_stderr = (
            f"--- attempt 1 (static) ---\n{first.stderr}\n"
            f"--- DHCP release ---\n{release.stderr}\n"
            f"--- attempt 2 (static after release) ---\n{retry.stderr}"
        )
        if retry.ok:
            _record_last_applied_ip(ctx, resolved.argv)
        return FixResult(
            ok=retry.ok,
            stdout=combined_stdout,
            stderr=combined_stderr,
            returncode=retry.returncode,
        )

    # disable_wifi: take the Wi-Fi adapter offline so it can't carry
    # 192.168.1.x test traffic. One netsh call (admin=disabled) — reliable on
    # any NIC and easy to reverse. We record the adapter so a normal app close
    # (and Reset) re-enables it, after which Windows auto-reconnects to the
    # saved network on its own.
    if action.id == "disable_wifi":
        resolved_wifi = resolve_disable_wifi(ctx)
        result = _run_argv(resolved_wifi.argv)
        if ctx is not None and not resolved_wifi.fallback and result.ok:
            adapter = None
            for token in resolved_wifi.argv:
                if token.startswith("name="):
                    adapter = token[len("name="):]
                    break
            if adapter:
                try:
                    ctx.run_state.wifi_offline_adapter = adapter
                except Exception:  # noqa: BLE001 - bookkeeping never breaks the fix
                    pass
        return result

    # The composite iperf3 fix: also create the UDP rule.
    if action.id == "open_iperf3_ports":
        first = _run_argv(action.argv)
        if not first.ok:
            return first
        udp = _run_argv(FIX_ACTIONS["open_iperf3_udp"].argv)
        return FixResult(
            ok=first.ok and udp.ok,
            stdout=first.stdout + "\n--- UDP rule ---\n" + udp.stdout,
            stderr=first.stderr + udp.stderr,
            returncode=udp.returncode if first.ok else first.returncode,
        )

    return _run_argv(action.argv)


def _run_argv(argv: list[str]) -> FixResult:
    try:
        proc = subprocess.run(
            # Pin netsh/etc. to their absolute System32 path — this runs
            # elevated, so a PATH/cwd-planted system tool would be admin RCE.
            harden_argv(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return FixResult(ok=False, stdout="", stderr=str(exc), returncode=-1)
    return FixResult(
        ok=(proc.returncode == 0),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


def _restart_as_admin() -> FixResult:
    """Launch a new elevated instance of PingPair and exit the current one."""
    if sys.platform != "win32":
        return FixResult(
            ok=False,
            stdout="",
            stderr="restart_as_admin is Windows-only.",
            returncode=-1,
        )
    # In a frozen PyInstaller build ``sys.executable`` is the PingPair
    # .exe itself, which has no ``-m`` switch — relaunch it with no
    # parameters. In a dev run ``sys.executable`` is the Python
    # interpreter, so it needs ``-m pingpair`` to find the package.
    params = None if getattr(sys, "frozen", False) else "-m pingpair"
    try:
        # ShellExecuteW with verb 'runas' triggers the UAC prompt.
        rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", sys.executable, params, None, 1
        )
    except (AttributeError, OSError) as exc:
        return FixResult(ok=False, stdout="", stderr=str(exc), returncode=-1)

    # ShellExecuteW returns >32 on success per Microsoft docs.
    ok = int(rc) > 32
    return FixResult(
        ok=ok,
        stdout=f"ShellExecuteW returned {rc}",
        stderr="" if ok else "Elevation cancelled or failed.",
        returncode=0 if ok else -1,
    )
