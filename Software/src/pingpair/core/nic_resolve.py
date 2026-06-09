"""Effective NIC configuration resolver — Group F (Q1, 2026-05-16).

The single source of truth for "what IP / subnet / gateway should this PC's
NIC actually use right now?". Called by:

* :func:`core.prereq.check_nic_ip` (task #24) — to decide what IP the
  Local NIC IP prereq check expects.
* :func:`core.fix_actions.resolve_set_static_ip` (task #10) — to build
  the netsh command that applies the right values.
* :mod:`views.setup_view` (tasks #21, #22) — to render the effective
  config in the role banner / hint placeholders.
* External-IP-change detection (task #23) — to compare ``last_applied_ip``
  against the currently-bound IP for this role.

Resolution order:

1. **Role.LOOPBACK** — dev mode. Returns ``127.0.0.1 / 255.0.0.0 / None``
   regardless of any override. The override is meaningless for loopback.
2. **Role.SERVER / Role.CLIENT** with ``override.use_custom == True`` —
   each of (IP, Subnet, Gateway) reads from the override if its field
   is non-empty, falling back to the profile per-field. Lets the user
   override just the gateway without re-typing the IP, etc.
3. **Role.SERVER / Role.CLIENT** with ``override.use_custom == False`` —
   profile defaults: ``cfg.network.<role>_ip`` /
   ``cfg.network.subnet_mask`` / ``cfg.network.gateway``.
4. **Role.UNDECIDED** — returns the profile's Server defaults as a safe
   placeholder. Matches the legacy behaviour from before
   ``check_nic_ip`` was made role-aware.

The result is a frozen :class:`EffectiveNic` dataclass so the views can
treat it like an immutable value object — no concerns about a caller
mutating it after read.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from ..config import AppConfig
from ..context import NicOverride, Role


@dataclass(frozen=True, slots=True)
class EffectiveNic:
    """Resolved NIC configuration for the current role + override pair.

    All three fields are strings (or None for ``gateway``) so that callers
    don't have to care whether they came from the typed-pydantic profile
    or from the user-typed Setup tab override. None on ``gateway`` means
    "no gateway / point-to-point LAN" — the canonical Test Procedure
    setup. Empty strings are normalised to None during resolution so
    downstream consumers can check ``is None`` uniformly.

    Fields:
    * ``ip`` — IPv4 address this PC's NIC should be bound to.
    * ``subnet_mask`` — IPv4 mask (always non-empty; defaults to
      ``255.255.255.0`` from the profile).
    * ``gateway`` — IPv4 gateway address, or None for no gateway.
    * ``source`` — Where the IP came from: ``"override"`` /
      ``"profile"`` / ``"loopback"``. Useful for the Setup tab status
      label ("Server role: 192.168.1.1 (from profile)" vs
      "Server role: 10.0.0.1 (custom)").
    """

    ip: str
    subnet_mask: str
    gateway: str | None
    source: str  # one of {"override", "profile", "loopback"}


def _first_non_empty(*values: str | None) -> str | None:
    """Return the first value that is non-empty (after strip), else None."""
    for v in values:
        if v is None:
            continue
        s = v.strip()
        if s:
            return s
    return None


def _valid_ipv4(value: str | None) -> str | None:
    """Return ``value`` (stripped) iff it's a valid IPv4 literal, else None.

    The Setup-tab NIC override is persisted from QSettings and is **not**
    pydantic-validated like the profile, yet it flows into an *elevated*
    ``netsh interface ipv4 set address`` call. Validating it here means a
    malformed/garbage override can never reach netsh — it falls back to the
    already-validated profile value instead. A subnet mask is itself a valid
    IPv4 literal, so this validates the mask field too.
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        ipaddress.IPv4Address(s)
    except ValueError:
        return None
    return s


def effective_nic_for_role(
    role: Role,
    cfg: AppConfig,
    override: NicOverride | None = None,
) -> EffectiveNic:
    """Resolve the effective NIC configuration for ``role``.

    See module docstring for the resolution order. ``override`` may be
    None (treated as "no override"), which is the default for the
    ``--check-prereqs`` CLI path that runs before any QSettings load.
    """
    # Loopback short-circuits — no override considered, no profile lookup.
    if role is Role.LOOPBACK:
        return EffectiveNic(
            ip="127.0.0.1",
            subnet_mask="255.0.0.0",
            gateway=None,
            source="loopback",
        )

    # Role-default IP from the profile. UNDECIDED returns Server defaults
    # as the safe fallback — matches the legacy lenient behaviour the
    # ``--check-prereqs`` CLI path relies on.
    if role is Role.CLIENT:
        profile_ip = str(cfg.network.client_ip)
    else:  # Role.SERVER or Role.UNDECIDED
        profile_ip = str(cfg.network.server_ip)

    profile_mask = str(cfg.network.subnet_mask)
    profile_gateway = str(cfg.network.gateway) if cfg.network.gateway else None

    # No override applied -> profile defaults.
    if override is None or not override.use_custom:
        return EffectiveNic(
            ip=profile_ip,
            subnet_mask=profile_mask,
            gateway=profile_gateway,
            source="profile",
        )

    # Override applied — each field falls back to the profile if the override
    # field is empty OR not a valid IPv4 literal (a garbage override must
    # never reach the elevated netsh call). This still lets the user override
    # just one piece (e.g. the gateway) without re-typing the others.
    eff_ip = _first_non_empty(_valid_ipv4(override.ip), profile_ip)
    eff_mask = _first_non_empty(_valid_ipv4(override.subnet), profile_mask)
    # Gateway is special: an explicit override of "" means "clear the
    # profile gateway too" (the user wants no gateway even if the
    # profile defines one). Distinguished from None which means "no
    # override was supplied". A non-empty but invalid gateway falls back
    # to the profile rather than being passed through.
    if override.gateway is None:
        eff_gateway = profile_gateway
    elif override.gateway.strip() == "":
        eff_gateway = None  # explicit clear
    else:
        eff_gateway = _valid_ipv4(override.gateway) or profile_gateway

    # Note: `eff_ip` and `eff_mask` are guaranteed non-None here because
    # _first_non_empty falls back to the profile values which are always
    # populated. The `or profile_ip` belt-and-braces handles the
    # impossible case of both override and profile being empty strings.
    return EffectiveNic(
        ip=eff_ip or profile_ip,
        subnet_mask=eff_mask or profile_mask,
        gateway=eff_gateway,
        # Source is "override" only when at least one override field
        # actually contributed a VALID value. If use_custom is on but every
        # field is empty/invalid, we effectively used the profile — be honest.
        source=(
            "override"
            if any(_valid_ipv4(v) for v in (override.ip, override.subnet, override.gateway))
            else "profile"
        ),
    )
