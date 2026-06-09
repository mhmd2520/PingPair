"""Prerequisite checks — phase 1.

Each public ``check_*`` function is a pure(ish) function that returns a
:class:`CheckResult`.  No globals, no Qt imports, no UI.  The view layer
calls :func:`run_checks` to get the full list and renders it; the
``--check-prereqs`` headless mode does the same and prints to stdout.

Cross-platform behaviour: this module is designed to run on a Linux dev
machine for tests.  Windows-only checks (firewall) return a SKIP result
on non-Windows so the test suite is green on the dev box.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from ..config import AppConfig
from ..context import NicOverride, Role
from .nic_resolve import effective_nic_for_role
from .winexec import harden_argv


# Windows: suppress the console window each probe subprocess would otherwise
# flash. Matters in a frozen (windowed) GUI build where a Setup tab refresh
# spawns several netsh / ping / -v probes. 0 is a no-op on other platforms.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"   # platform-specific check that doesn't apply


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    fix_action_id: str | None = None  # links into fix_actions.FIX_ACTIONS


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def check_python_version(minimum: tuple[int, int] = (3, 11)) -> CheckResult:
    """Python interpreter is recent enough for our type hint syntax."""
    have = sys.version_info[:2]
    have_str = f"{have[0]}.{have[1]}.{sys.version_info.micro}"
    if have >= minimum:
        return CheckResult(
            name="Python interpreter",
            status=Status.PASS,
            detail=f"Python {have_str} (≥ {minimum[0]}.{minimum[1]} required).",
        )
    return CheckResult(
        name="Python interpreter",
        status=Status.FAIL,
        detail=(
            f"Python {have_str} found, but {minimum[0]}.{minimum[1]}+ is required. "
            "Install from python.org or via `winget install Python.Python.3.12`."
        ),
    )


def check_ethernet_cable(role: Role = Role.UNDECIDED) -> CheckResult:
    """Physical Ethernet link is up — the FIRST thing to verify.

    The whole 20-case test rides a point-to-point Ethernet cable between the
    two laptops. If that cable is unplugged (or the wired NIC is disabled),
    Windows reports "Network cable unplugged" and *every* later check — IP
    binding, gateway reachability, the control channel — fails in confusing,
    secondary ways. Surfacing the cable first means the user is told plainly
    to plug it in before chasing IP / firewall ghosts.

    Detection: psutil's ``net_if_stats()[nic].isup`` is ``False`` when the
    wired adapter has no carrier (cable out) **or** is administratively down
    — both mean "no usable link", which is exactly what we want to flag.
    The adapter itself is chosen by the same
    :func:`core.fix_actions.detect_primary_adapter` the IP-fix uses, so the
    cable check and the "Set the correct IP" fix always agree on which NIC is
    the test link. (A cable-out adapter still appears in ``net_if_addrs`` via
    its MAC entry, so it's still found.)

    * ``Role.LOOPBACK`` -> SKIP (loopback runs entirely on 127.0.0.1).
    * psutil missing -> WARN (can't enumerate adapters).
    * No wired Ethernet adapter found -> FAIL (enable / connect one).
    * Wired adapter present but link down -> FAIL (plug the cable in).
    * Wired adapter with link up -> PASS.

    No ``fix_action_id`` — a physical cable can't be plugged in by software.
    """
    if role is Role.LOOPBACK:
        return CheckResult(
            name="Ethernet cable",
            status=Status.SKIP,
            detail="Loopback dev mode runs on 127.0.0.1 — no cable needed.",
        )
    try:
        import psutil  # lazy import so tests can monkeypatch
    except ImportError:
        return CheckResult(
            name="Ethernet cable",
            status=Status.WARN,
            detail="psutil not installed; cannot detect the Ethernet link.",
        )

    from .fix_actions import detect_primary_adapter

    adapter = detect_primary_adapter()
    if adapter is None:
        return CheckResult(
            name="Ethernet cable",
            status=Status.FAIL,
            detail=(
                "No wired Ethernet adapter found. Enable the port or plug in "
                "a USB-Ethernet dongle, then Re-check."
            ),
        )

    try:
        stats = psutil.net_if_stats()
    except Exception:  # psutil can raise OSError on odd NICs
        stats = {}
    st = stats.get(adapter)
    if st is not None and not st.isup:
        return CheckResult(
            name="Ethernet cable",
            status=Status.FAIL,
            detail=(
                f'"{adapter}" has no link — cable unplugged or adapter '
                "disabled. Reconnect, then Re-check."
            ),
        )
    return CheckResult(
        name="Ethernet cable",
        status=Status.PASS,
        detail=f'"{adapter}" link is up — cable connected.',
    )


def _is_ipv4(addr: object) -> bool:
    """True if a psutil ``snicaddr`` is an IPv4 address.

    The ``AddressFamily`` enum's ``.name`` repr varies across platforms and
    Python builds (``"AF_INET"`` vs ``"AddressFamily.AF_INET"``), so fall back
    to the numeric family value (``AF_INET == 2``). One copy used by every
    adapter-enumeration site (here + :mod:`core.fix_actions`).
    """
    family = getattr(addr, "family", None)
    if getattr(family, "name", None) in {"AF_INET", "AddressFamily.AF_INET"}:
        return True
    return int(getattr(family, "value", -1)) == 2


def check_nic_ip(
    cfg: AppConfig,
    role: Role = Role.UNDECIDED,
    override: NicOverride | None = None,
) -> CheckResult:
    """Local machine has the canonical IP for the current role.

    Role-aware so the prereq table tells the same story as the orange
    role-mismatch banner (driven by
    :func:`core.role_detect.evaluate_role_ip_warning`). Before this was
    role-aware, the check returned PASS whenever EITHER canonical IP was
    bound — even when the saved role disagreed with what was on the wire
    (e.g. role=Client but IP=192.168.1.1). The banner said "fix me" but
    the prereq said "green" — confusing.

    Override-aware (Group F, Q1, 2026-05-16): when ``override`` is supplied
    and its ``use_custom`` flag is True, the expected IP comes from the
    user's per-PC Setup tab override instead of the profile default. The
    resolution is delegated to :func:`core.nic_resolve.effective_nic_for_role`
    — the single source of truth shared with the netsh fix and the Setup tab
    banner. Detail string makes the source visible: "Server role: …" vs
    "Server role (custom): …".

    Behaviour matrix:

    * ``Role.SERVER`` -> require the effective Server IP bound; FAIL with
      ``set_static_ip`` fix otherwise (even if the other role's canonical IP
      happens to be bound on another adapter).
    * ``Role.CLIENT`` -> mirror: require the effective Client IP.
    * ``Role.LOOPBACK`` -> dev mode; PASS regardless of NIC layout.
    * ``Role.UNDECIDED`` -> the original lenient check kept for the
      ``--check-prereqs`` CLI path that runs before any role is loaded
      from QSettings — either canonical IP satisfies; FAIL only when
      neither is bound.
    """
    expected_server = str(cfg.network.server_ip)
    expected_client = str(cfg.network.client_ip)
    # Effective IP for the active role — override beats profile when active.
    eff = effective_nic_for_role(role, cfg, override)
    is_custom = eff.source == "override"
    custom_tag = " (custom)" if is_custom else ""

    if role is Role.LOOPBACK:
        return CheckResult(
            name="Local NIC IP",
            status=Status.PASS,
            detail="Loopback dev mode — IP check skipped.",
        )

    try:
        import psutil  # imported lazily so tests can monkeypatch
    except ImportError:
        return CheckResult(
            name="Local NIC IP",
            status=Status.WARN,
            detail="psutil not installed; cannot enumerate adapters.",
        )

    addrs_by_iface = psutil.net_if_addrs()
    bound_ips: set[str] = set()
    all_ipv4: list[tuple[str, str]] = []

    for iface, addrs in addrs_by_iface.items():
        for addr in addrs:
            if not _is_ipv4(addr):
                continue
            ip = addr.address
            all_ipv4.append((iface, ip))
            bound_ips.add(ip)

    def _iface_with(target: str) -> str | None:
        for iface, ip in all_ipv4:
            if ip == target:
                return iface
        return None

    summary = ", ".join(f"{i}={a}" for i, a in all_ipv4) or "no IPv4 addresses"

    if role is Role.SERVER:
        target = eff.ip
        iface = _iface_with(target)
        if iface:
            return CheckResult(
                name="Local NIC IP",
                status=Status.PASS,
                detail=f"Server role{custom_tag}: {iface} has {target}.",
            )
        # When the OTHER role's profile IP is bound, mention it — common
        # misconfiguration where the user picked the wrong role.
        if not is_custom and expected_client in bound_ips:
            wrong_iface = _iface_with(expected_client) or "?"
            why = (
                f"Server role expects {target}, but {wrong_iface} has "
                f"{expected_client} (Client IP). Use Set the correct IP, "
                "or switch role above."
            )
        else:
            why = (
                f"Server role{custom_tag} expects {target}, not bound. "
                f"Currently: {summary}."
            )
        return CheckResult(
            name="Local NIC IP",
            status=Status.FAIL,
            detail=why,
            fix_action_id="set_static_ip",
        )

    if role is Role.CLIENT:
        target = eff.ip
        iface = _iface_with(target)
        if iface:
            return CheckResult(
                name="Local NIC IP",
                status=Status.PASS,
                detail=f"Client role{custom_tag}: {iface} has {target}.",
            )
        if not is_custom and expected_server in bound_ips:
            wrong_iface = _iface_with(expected_server) or "?"
            why = (
                f"Client role expects {target}, but {wrong_iface} has "
                f"{expected_server} (Server IP). Use Set the correct IP, "
                "or switch role above."
            )
        else:
            why = (
                f"Client role{custom_tag} expects {target}, not bound. "
                f"Currently: {summary}."
            )
        return CheckResult(
            name="Local NIC IP",
            status=Status.FAIL,
            detail=why,
            fix_action_id="set_static_ip",
        )

    # Role.UNDECIDED — legacy lenient behaviour (CLI --check-prereqs path).
    if expected_server in bound_ips:
        iface = _iface_with(expected_server)
        return CheckResult(
            name="Local NIC IP",
            status=Status.PASS,
            detail=f"Server role detected: {iface} has {expected_server}.",
        )
    if expected_client in bound_ips:
        iface = _iface_with(expected_client)
        return CheckResult(
            name="Local NIC IP",
            status=Status.PASS,
            detail=f"Client role detected: {iface} has {expected_client}.",
        )
    return CheckResult(
        name="Local NIC IP",
        status=Status.FAIL,
        detail=(
            f"Neither {expected_server} (Server) nor {expected_client} (Client) "
            f"is bound. Currently: {summary}."
        ),
        fix_action_id="set_static_ip",
    )


# Firewall rule names we own.  Uniqueness lets us check by name.
FIREWALL_RULE_NAMES = {
    "icmp": "PingPair ICMP echo (in)",
    "iperf3_tcp": "PingPair iperf3 TCP 5201 (in)",
    "iperf3_udp": "PingPair iperf3 UDP 5201 (in)",
    "control": "PingPair control TCP 5202 (in)",
}


def _netsh_rule_exists(rule_name: str) -> tuple[bool, str]:
    """Return (exists, raw_output) for a single firewall rule lookup."""
    try:
        proc = subprocess.run(
            harden_argv(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name={rule_name}"]
            ),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"netsh failed: {exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    # netsh prints "No rules match the specified criteria." (en-US) when missing.
    # On non-English locales it's localised, so also key off the absence of
    # the typical "Rule Name:" / "Enabled:" preamble.
    missing = (
        "No rules match" in output
        or "no rules match" in output
        or not re.search(r"^\s*(Rule Name|Enabled):", output, re.MULTILINE)
    )
    return (not missing), output


def _check_firewall_rule(
    friendly: str, rule_id: str, fix_id: str, role: Role = Role.UNDECIDED
) -> CheckResult:
    if role is Role.LOOPBACK:
        return CheckResult(
            name=friendly,
            status=Status.SKIP,
            detail="Loopback dev mode runs on 127.0.0.1 — firewall rule not needed.",
        )
    if sys.platform != "win32":
        return CheckResult(
            name=friendly,
            status=Status.SKIP,
            detail=f"Non-Windows platform ({sys.platform}); firewall check skipped.",
        )
    rule_name = FIREWALL_RULE_NAMES[rule_id]
    exists, _raw = _netsh_rule_exists(rule_name)
    if exists:
        return CheckResult(
            name=friendly,
            status=Status.PASS,
            detail=f'Firewall rule "{rule_name}" is present.',
        )
    return CheckResult(
        name=friendly,
        status=Status.WARN,
        detail=(
            f'No explicit rule "{rule_name}". Built-in rules may still '
            "allow it; click Fix to add one."
        ),
        fix_action_id=fix_id,
    )


def check_firewall_icmp(role: Role = Role.UNDECIDED) -> CheckResult:
    return _check_firewall_rule(
        friendly="Firewall: ICMP echo (ping)",
        rule_id="icmp",
        fix_id="open_icmp",
        role=role,
    )


def check_firewall_iperf3(role: Role = Role.UNDECIDED) -> CheckResult:
    """One row that combines TCP + UDP on 5201 since they're always paired."""
    if role is Role.LOOPBACK:
        return CheckResult(
            name="Firewall: iperf3 (TCP/UDP 5201)",
            status=Status.SKIP,
            detail="Loopback dev mode runs on 127.0.0.1 — firewall rules not needed.",
        )
    if sys.platform != "win32":
        return CheckResult(
            name="Firewall: iperf3 (TCP/UDP 5201)",
            status=Status.SKIP,
            detail=f"Non-Windows platform ({sys.platform}); firewall check skipped.",
        )
    tcp_exists, _ = _netsh_rule_exists(FIREWALL_RULE_NAMES["iperf3_tcp"])
    udp_exists, _ = _netsh_rule_exists(FIREWALL_RULE_NAMES["iperf3_udp"])
    if tcp_exists and udp_exists:
        return CheckResult(
            name="Firewall: iperf3 (TCP/UDP 5201)",
            status=Status.PASS,
            detail="Both TCP and UDP rules for port 5201 are present.",
        )
    missing = []
    if not tcp_exists:
        missing.append("TCP")
    if not udp_exists:
        missing.append("UDP")
    return CheckResult(
        name="Firewall: iperf3 (TCP/UDP 5201)",
        status=Status.WARN,
        detail=f"Missing rule(s): {', '.join(missing)}. Click Fix to add them.",
        fix_action_id="open_iperf3_ports",
    )


def check_firewall_control(role: Role = Role.UNDECIDED) -> CheckResult:
    return _check_firewall_rule(
        friendly="Firewall: control channel (TCP 5202)",
        rule_id="control",
        fix_id="open_control_port",
        role=role,
    )


# Adapter-name prefixes that count as Wi-Fi for the off-check.
# Single source of truth — fix_actions.detect_wifi_adapter() imports
# this so detection (here) and remediation (there) always agree on
# what counts as Wi-Fi. Distinct from fix_actions._ADAPTER_SKIP_PREFIXES,
# which is broader (also blacklists virtual switches, Bluetooth, etc.).
_WIFI_NAME_PREFIXES: tuple[str, ...] = ("Wi-Fi", "Wireless", "WLAN")


def _wifi_adapters_with_ipv4() -> list[tuple[str, str]]:
    """Return [(adapter_name, ipv4)] for any Wi-Fi-named NIC carrying an IPv4.

    "Carrying an IPv4" means a non-loopback / non-APIPA address — a Wi-Fi
    radio that's *enabled but not associated with any AP* doesn't show
    up here, and that's the right semantics: only flag Wi-Fi adapters
    that could realistically steal test traffic away from the dedicated
    Ethernet link.
    """
    try:
        import psutil  # lazy import so tests can monkeypatch
    except ImportError:
        return []

    # A disabled / down adapter can't carry traffic even if a stale IPv4
    # lingers in net_if_addrs for a moment — skip it so the check flips to
    # PASS right after the "Disconnect Wi-Fi" fix disables the adapter.
    try:
        stats = psutil.net_if_stats()
    except Exception:  # noqa: BLE001
        stats = {}

    # net_if_addrs() can raise OSError on odd NICs (same hazard guarded in
    # _psutil_ipv4 / check_ethernet_cable). Returning [] keeps both callers
    # safe — the Run-tab Wi-Fi hard block (an uncaught Qt slot) must never
    # wedge the Run button, and check_wifi_off degrades to "no Wi-Fi seen".
    try:
        addrs_by_iface = psutil.net_if_addrs()
    except Exception:  # noqa: BLE001
        return []

    found: list[tuple[str, str]] = []
    for iface, addrs in addrs_by_iface.items():
        if not any(iface.startswith(p) for p in _WIFI_NAME_PREFIXES):
            continue
        st = stats.get(iface)
        if st is not None and not st.isup:
            continue
        for addr in addrs:
            if not _is_ipv4(addr):
                continue
            ip = addr.address
            # Skip loopback (127.x.x.x) and APIPA (169.254.x.x) — those
            # aren't real associations.
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            found.append((iface, ip))
    return found


def wifi_on_test_subnet(
    server_ip: str,
    subnet_mask: str,
    wifi_adapters: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Pure: which Wi-Fi ``(adapter, ipv4)`` pairs sit on the *test* subnet.

    The canonical point-to-point setup puts the Server and Client on one
    subnet (``server_ip`` masked by ``subnet_mask`` — e.g.
    ``192.168.1.0/24``). A Wi-Fi NIC holding an IPv4 in that *same* subnet
    gives Windows a second, competing route to the server IP, so
    iperf3/fping traffic can egress over Wi-Fi instead of the dedicated
    Ethernet link and silently corrupt the measurement. (A Wi-Fi NIC on a
    *different* subnet — ordinary home Wi-Fi — can't: the route to the
    server IP still resolves to Ethernet, and the ``-B``/``-S`` source
    binds pin the source to the Ethernet IP.)

    Returns the offending pairs (empty = safe). Unparseable input (a bad
    mask or IP) is treated as "no conflict" so a typo can never wedge the
    Run tab — the looser :func:`check_wifi_off` WARN still flags a live
    Wi-Fi NIC in that case.
    """
    try:
        net = ipaddress.ip_network(f"{server_ip}/{subnet_mask}", strict=False)
    except ValueError:
        return []
    hits: list[tuple[str, str]] = []
    for adapter, ip in wifi_adapters:
        try:
            if ipaddress.ip_address(ip) in net:
                hits.append((adapter, ip))
        except ValueError:
            continue
    return hits


def wifi_adapters_on_test_subnet(cfg: AppConfig) -> list[tuple[str, str]]:
    """Live: Wi-Fi NICs whose IPv4 shares the test subnet (would steal traffic).

    Windows-only (mirrors :func:`check_wifi_off`); returns ``[]`` elsewhere
    and whenever there's no conflict. The Run tab uses a non-empty result to
    **hard-block** a sweep — unlike the advisory :func:`check_wifi_off` WARN
    (which fires for *any* live Wi-Fi NIC), this narrows to the genuinely
    traffic-stealing case: Wi-Fi on the same subnet as the link under test.
    """
    if sys.platform != "win32":
        return []
    return wifi_on_test_subnet(
        str(cfg.network.server_ip),
        cfg.network.subnet_mask,
        _wifi_adapters_with_ipv4(),
    )


def check_gateway_reachable(
    cfg: AppConfig,
    role: Role = Role.UNDECIDED,
    override: NicOverride | None = None,
) -> CheckResult:
    """Effective gateway responds to ICMP. Optional — Group F (Q1).

    The Test Procedure canonical setup is point-to-point with no gateway.
    When a user configures a gateway (either via the profile or the
    per-PC override) we want a quick sanity check that the gateway is
    actually reachable — otherwise the test traffic will silently take
    the wrong path.

    Returns:

    * **SKIP** when no effective gateway is configured (the canonical
      no-gateway case) OR when the role is Loopback/Undecided. Also
      SKIPs on non-Windows because the ping invocation below uses the
      Windows ``ping`` flag set.
    * **PASS** when the gateway responds to a 1-packet ping within ~2 s.
    * **WARN** (not FAIL) when the gateway doesn't respond. A missing
      gateway is annoying but not fatal — the user may have just typed
      the wrong IP. Test execution still works for any LAN that doesn't
      cross the gateway.
    """
    from .nic_resolve import effective_nic_for_role

    if sys.platform != "win32":
        return CheckResult(
            name="Gateway reachable",
            status=Status.SKIP,
            detail=f"Gateway ping is Windows-only ({sys.platform}).",
        )
    if role in (Role.LOOPBACK, Role.UNDECIDED):
        return CheckResult(
            name="Gateway reachable",
            status=Status.SKIP,
            detail="No gateway check in this role.",
        )
    eff = effective_nic_for_role(role, cfg, override)
    if not eff.gateway:
        return CheckResult(
            name="Gateway reachable",
            status=Status.SKIP,
            detail="No gateway configured (point-to-point LAN).",
        )
    # Windows ping: -n 1 (one packet) -w 2000 (2 s deadline).
    try:
        proc = subprocess.run(
            harden_argv(["ping", "-n", "1", "-w", "2000", eff.gateway]),
            capture_output=True, text=True, errors="replace",
            timeout=5, check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name="Gateway reachable",
            status=Status.WARN,
            detail=f"Could not ping {eff.gateway}: {exc}",
        )
    if proc.returncode == 0:
        return CheckResult(
            name="Gateway reachable",
            status=Status.PASS,
            detail=f"{eff.gateway} responded to ping.",
        )
    return CheckResult(
        name="Gateway reachable",
        status=Status.WARN,
        detail=(
            f"{eff.gateway} did not respond. Check the gateway IP, or "
            "clear it for a point-to-point LAN."
        ),
    )


def check_wifi_off(role: Role = Role.UNDECIDED) -> CheckResult:
    """Warn if any Wi-Fi adapter is carrying a real IPv4 — routes test
    traffic away from the dedicated Ethernet point-to-point link and
    produces phantom packet loss in reports.

    * ``Role.LOOPBACK`` -> SKIP (127.0.0.1 traffic never leaves the host,
      so a live Wi-Fi adapter can't steal it).
    * Non-Windows -> SKIP (the disable_wifi fix shells out to netsh).
    * No Wi-Fi adapter with non-loopback IPv4 -> PASS.
    * One or more Wi-Fi adapters carrying IPv4 -> WARN with the
      ``disable_wifi`` fix.
    """
    if role is Role.LOOPBACK:
        return CheckResult(
            name="Wi-Fi disabled",
            status=Status.SKIP,
            detail="Loopback dev mode runs on 127.0.0.1 — Wi-Fi can't affect the test.",
        )
    if sys.platform != "win32":
        return CheckResult(
            name="Wi-Fi disabled",
            status=Status.SKIP,
            detail=f"Wi-Fi adapter detection is Windows-only ({sys.platform}).",
        )

    # Reassurance shown in both states so a first-time user isn't worried
    # when their Wi-Fi icon disappears: PingPair always puts it back.
    _restore_note = " Auto re-enabled on close or Reset."
    adapters = _wifi_adapters_with_ipv4()
    if not adapters:
        return CheckResult(
            name="Wi-Fi disabled",
            status=Status.PASS,
            detail=(
                "No Wi-Fi adapter carrying IPv4 — test traffic uses the "
                "Ethernet link." + _restore_note
            ),
        )
    summary = ", ".join(f"{n} ({ip})" for n, ip in adapters)
    return CheckResult(
        name="Wi-Fi disabled",
        status=Status.WARN,
        detail=(
            f"Wi-Fi carrying IPv4: {summary}. May steal test traffic — "
            "click Disable Wi-Fi." + _restore_note
        ),
        fix_action_id="disable_wifi",
    )


def run_checks(
    cfg: AppConfig,
    role: Role = Role.UNDECIDED,
    override: NicOverride | None = None,
) -> list[CheckResult]:
    """Run every check in display order. Single source of truth for ordering.

    ``role`` is threaded through to the role-aware ``check_nic_ip`` so the
    prereq table FAILs (with a "Set the correct IP" fix) when the bound
    NIC IP doesn't match the saved role — keeping the table in sync with
    the orange role-mismatch banner. Defaults to :data:`Role.UNDECIDED`
    so the ``--check-prereqs`` CLI path (which runs before any role is
    loaded from QSettings) keeps its legacy lenient behaviour.

    ``override`` is the per-PC NIC override from the Setup tab (Group F /
    Q1, 2026-05-16). When supplied and active, the NIC IP check expects
    the override values instead of the profile defaults. Defaults to
    ``None`` so legacy CLI callers keep working without modification.
    """
    checkers: Iterable = (
        lambda: check_ethernet_cable(role),
        check_python_version,
        lambda: check_nic_ip(cfg, role, override),
        lambda: check_wifi_off(role),
        lambda: check_gateway_reachable(cfg, role, override),
        lambda: check_firewall_icmp(role),
        lambda: check_firewall_iperf3(role),
        lambda: check_firewall_control(role),
    )
    return [c() for c in checkers]


def has_blockers(results: Iterable[CheckResult]) -> bool:
    """A 'blocker' is anything FAIL — WARN/SKIP/PASS are all green-light."""
    return any(r.status is Status.FAIL for r in results)
