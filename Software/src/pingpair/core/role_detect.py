"""First-launch role auto-detection from local IPv4 addresses.

The user's preference, set on 2026-05-10, is hands-free first launch:
the canonical Test Procedure topology binds the Server to 192.168.1.1
and the Client to 192.168.1.2. Whichever IP this laptop holds when it
first starts the app *should* select the matching role automatically,
no dialog, no clicking.

This module is intentionally Qt-free so it can be unit-tested with
`pytest` without spawning a `QApplication`. The public surface:

* :func:`local_ipv4_addresses` — best-effort enumeration of bound IPv4s.
* :func:`detect_role_for_addresses` — pure: list of IPs + (server_ip,
  client_ip) -> :class:`Role`.
* :func:`detect_role` — first-launch policy: IP auto-detect, else Loopback.
* :func:`evaluate_role_ip_warning` — pure: returns the orange-banner
  text for the Setup tab, or "" when role + bound IPs are consistent.
  Re-run on every prereq refresh so the banner clears automatically
  when the user fixes the NIC IP via "Set the correct IP".
"""

from __future__ import annotations

import socket
from collections.abc import Iterable

from ..context import Role


def local_ipv4_addresses() -> list[str]:
    """Return all non-loopback IPv4 addresses bound to local interfaces.

    Uses psutil's live ``net_if_addrs`` query when available — that hits
    the OS adapter table directly with no caching, so a freshly-applied
    netsh static IP shows up immediately on the next call.

    The previous implementation used ``socket.getaddrinfo(gethostname())``,
    which on Windows caches the hostname→IP mapping in the DNS resolver
    for ~30 s. That made the orange "IP doesn't match role" banner lag
    on the first prereq pass after a successful IP fix — the user had
    to click Re-check to see it clear. (#13 follow-up 2026-05-12)

    Falls back to the socket-based query when psutil is missing (e.g.
    on the Linux test sandbox).

    Best-effort — any exception returns an empty list rather than
    raising, so callers can fall back to the saved choice.
    """
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]

    addrs: set[str] = set()
    if psutil is not None:
        try:
            for iface_addrs in psutil.net_if_addrs().values():
                for addr in iface_addrs:
                    ip = str(addr.address)
                    # Skip loopback and APIPA (169.254.x.x) — a link-local
                    # address never matches a canonical role IP and only
                    # adds noise to the role-warning banner's bound list.
                    if (
                        "." in ip
                        and not ip.startswith("127.")
                        and not ip.startswith("169.254.")
                    ):
                        addrs.add(ip)
            return sorted(addrs)
        except Exception:  # noqa: BLE001
            pass

    # Fallback: socket-based, with the DNS-cache caveat noted above.
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ip = str(info[4][0])
            if (
                ip
                and not ip.startswith("127.")
                and not ip.startswith("169.254.")
            ):
                addrs.add(ip)
    except (socket.gaierror, OSError):
        return []
    return sorted(addrs)


def detect_role_for_addresses(
    local_ips: Iterable[str],
    *,
    server_ip: str,
    client_ip: str,
) -> Role:
    """Pure mapping: which role do these local IPs imply?

    Rules (first match wins):

    1. ``server_ip`` is bound here -> :data:`Role.SERVER`.
    2. ``client_ip`` is bound here -> :data:`Role.CLIENT`.
    3. Otherwise -> :data:`Role.CLIENT` (safe fallback — Client is the
       side that initiates, so a misconfigured Client just fails to
       connect rather than silently listening forever).
    """
    bound = {ip for ip in local_ips if ip}
    if server_ip in bound:
        return Role.SERVER
    if client_ip in bound:
        return Role.CLIENT
    return Role.CLIENT


def detect_role(
    *,
    server_ip: str,
    client_ip: str,
    bound_ips: Iterable[str] | None = None,
) -> tuple[Role, bool]:
    """Top-level first-launch role policy used by ``app.launch_gui``.

    A NIC bound to a canonical test IP picks that role
    (``192.168.1.1`` -> Server, ``192.168.1.2`` -> Client). When
    **neither** is bound, a fresh install opens in :data:`Role.LOOPBACK`
    — self-contained single-PC mode — so the user can run a sweep
    immediately without wiring up a second laptop. (Changed 2026-06-02:
    the no-match default used to be Client.)

    Returns ``(role, matched)`` — *matched* is True only when the role
    came from a positive IP hit, False when it fell through to the
    Loopback default. The GUI uses *matched* to log how the role was
    chosen; the amber Loopback role banner + the welcome tour explain
    the self-contained mode, so no "couldn't auto-detect" warning is
    raised on the default path (Loopback has no IP to mismatch).

    ``bound_ips`` is injectable for tests; production omits it and the
    live :func:`local_ipv4_addresses` query is used.
    """
    ips = list(bound_ips) if bound_ips is not None else local_ipv4_addresses()
    role = detect_role_for_addresses(ips, server_ip=server_ip, client_ip=client_ip)
    # "matched" means the role came from a positive IP hit, not the
    # fallback. Test both sides explicitly rather than trusting
    # "role is SERVER" — that stays correct even if the fallback in
    # detect_role_for_addresses ever changes.
    matched = (
        (role is Role.SERVER and server_ip in ips)
        or (role is Role.CLIENT and client_ip in ips)
    )
    if not matched:
        return Role.LOOPBACK, False
    return role, True


def evaluate_role_ip_warning(
    *,
    role: Role,
    bound_ips: Iterable[str],
    server_ip: str,
    client_ip: str,
) -> str:
    """Pure helper: does the saved ``role`` match the bound local IPs?

    Returns the warning string to render on the Setup tab banner, or
    ``""`` if there's nothing to warn about. Used both at app launch
    (initial check) and on every prereq refresh (so the banner clears
    automatically when the user fixes the NIC IP).

    * Server / Client roles must have their canonical IP bound.
    * Loopback and Undecided never warn — Loopback is dev mode and
      Undecided means the user hasn't picked yet.
    """
    bound = {ip for ip in bound_ips if ip}
    bound_summary = ", ".join(sorted(bound)) or "(none)"

    if role is Role.SERVER and server_ip not in bound:
        return (
            f"This PC's IP doesn't match the saved Server role - "
            f"expected {server_ip} to be bound, but it isn't. "
            f"Currently bound: {bound_summary}. "
            "Either fix the NIC IP below or change the role above."
        )
    if role is Role.CLIENT and client_ip not in bound:
        return (
            f"This PC's IP doesn't match the saved Client role - "
            f"expected {client_ip} to be bound, but it isn't. "
            f"Currently bound: {bound_summary}. "
            "Either fix the NIC IP below or change the role above."
        )
    return ""


def detect_external_ip_change(
    role: Role,
    expected_ip: str,
    bound_ips: list[str],
    last_applied: str | None,
) -> tuple[str, str] | None:
    """Detect that the bound NIC IP changed outside PingPair's control.

    Group F (Q1, 2026-05-16): drives the "External IP change detected"
    dialog (task #23). Returns ``(was, now)`` to surface in the dialog
    when:

    * The role is Server or Client (Loopback/Undecided never report
      divergence — the override doesn't apply there).
    * ``last_applied`` is non-empty (we've successfully applied an IP
      via netsh at least once — otherwise there's no baseline to
      compare against, and the user must have configured the IP
      manually before launching us).
    * The ``expected_ip`` (= effective IP for this role) is NOT
      currently bound, AND the last_applied IP is ALSO not bound
      (i.e. the value the user previously set has actively disappeared
      — DHCP reclaim, manual netsh, NIC reset).
    * The currently-bound IP is different from both last_applied and
      expected (a "stranger" IP showed up).

    Returns ``None`` when there's no divergence to surface. The view
    layer treats None as "nothing to do" and skips the dialog.

    Pure helper so the prompt logic can be unit-tested without a
    QApplication.
    """
    if role not in (Role.SERVER, Role.CLIENT):
        return None
    if not last_applied:
        return None
    bound = {ip for ip in bound_ips if ip}
    # No divergence if either the expected OR the last_applied IP is
    # still bound — the world hasn't actually changed in a confusing way.
    if expected_ip in bound or last_applied in bound:
        return None
    # The bound IPs (excluding loopback / APIPA) are the candidates for
    # "current". Pick the first non-link-local IPv4.
    current = ""
    for ip in sorted(bound):
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        current = ip
        break
    if not current:
        return None
    return (last_applied, current)

