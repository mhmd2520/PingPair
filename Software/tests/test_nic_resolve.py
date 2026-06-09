"""Tests for :func:`core.nic_resolve.effective_nic_for_role` (Group F, Q1).

The resolver is the single source of truth used by check_nic_ip, the
netsh fix, the Setup-tab banner, and the external-IP-change detection
dialog. Every branch of the resolution order needs an assertion so
future refactors stay honest.
"""

from __future__ import annotations

from pingpair.config import load_default_config
from pingpair.context import NicOverride, Role
from pingpair.core.nic_resolve import EffectiveNic, effective_nic_for_role


# ---------------------------------------------------------------------------
# Role.LOOPBACK — override is ignored, returns 127.0.0.1 unconditionally
# ---------------------------------------------------------------------------


def test_loopback_ignores_override_and_returns_127() -> None:
    cfg = load_default_config()
    override = NicOverride(use_custom=True, ip="10.0.0.5", subnet="255.0.0.0", gateway="10.0.0.1")
    eff = effective_nic_for_role(Role.LOOPBACK, cfg, override)
    assert eff.ip == "127.0.0.1"
    assert eff.subnet_mask == "255.0.0.0"
    assert eff.gateway is None
    assert eff.source == "loopback"


def test_loopback_with_no_override() -> None:
    cfg = load_default_config()
    eff = effective_nic_for_role(Role.LOOPBACK, cfg, None)
    assert eff.ip == "127.0.0.1"
    assert eff.source == "loopback"


# ---------------------------------------------------------------------------
# Role.SERVER / Role.CLIENT — no override -> profile defaults
# ---------------------------------------------------------------------------


def test_server_no_override_uses_profile_defaults() -> None:
    cfg = load_default_config()
    eff = effective_nic_for_role(Role.SERVER, cfg, None)
    assert eff.ip == "192.168.1.1"
    assert eff.subnet_mask == "255.255.255.0"
    assert eff.gateway is None   # defaults.json ships gateway: null
    assert eff.source == "profile"


def test_client_no_override_uses_profile_defaults() -> None:
    cfg = load_default_config()
    eff = effective_nic_for_role(Role.CLIENT, cfg, None)
    assert eff.ip == "192.168.1.2"
    assert eff.source == "profile"


def test_undecided_falls_back_to_server_defaults() -> None:
    """UNDECIDED is what --check-prereqs uses before any QSettings load."""
    cfg = load_default_config()
    eff = effective_nic_for_role(Role.UNDECIDED, cfg, None)
    assert eff.ip == "192.168.1.1"
    assert eff.source == "profile"


def test_override_with_use_custom_false_still_uses_profile() -> None:
    """User typed values into the override fields but never ticked the
    master checkbox. The override is dormant; profile rules."""
    cfg = load_default_config()
    override = NicOverride(use_custom=False, ip="10.0.0.5")
    eff = effective_nic_for_role(Role.SERVER, cfg, override)
    assert eff.ip == "192.168.1.1"   # profile wins
    assert eff.source == "profile"


# ---------------------------------------------------------------------------
# Override active — full + partial overrides
# ---------------------------------------------------------------------------


def test_full_override_replaces_every_field() -> None:
    cfg = load_default_config()
    override = NicOverride(
        use_custom=True,
        ip="10.0.0.5",
        subnet="255.255.0.0",
        gateway="10.0.0.1",
    )
    eff = effective_nic_for_role(Role.CLIENT, cfg, override)
    assert eff.ip == "10.0.0.5"
    assert eff.subnet_mask == "255.255.0.0"
    assert eff.gateway == "10.0.0.1"
    assert eff.source == "override"


def test_partial_override_ip_only_keeps_profile_subnet() -> None:
    """User overrode just the IP; subnet + gateway fall back to profile."""
    cfg = load_default_config()
    override = NicOverride(use_custom=True, ip="10.0.0.5", subnet=None, gateway=None)
    eff = effective_nic_for_role(Role.SERVER, cfg, override)
    assert eff.ip == "10.0.0.5"
    assert eff.subnet_mask == "255.255.255.0"   # profile default
    assert eff.gateway is None                  # profile null
    assert eff.source == "override"


def test_partial_override_gateway_only_keeps_profile_ip() -> None:
    """User overrode just the gateway."""
    cfg = load_default_config()
    override = NicOverride(use_custom=True, ip="", subnet="", gateway="192.168.1.254")
    eff = effective_nic_for_role(Role.SERVER, cfg, override)
    assert eff.ip == "192.168.1.1"      # profile
    assert eff.subnet_mask == "255.255.255.0"
    assert eff.gateway == "192.168.1.254"
    assert eff.source == "override"


def test_override_use_custom_true_but_all_fields_empty_reports_profile_source() -> None:
    """Master checkbox ticked but the user cleared every field. The
    effective config matches the profile, so ``source`` is "profile"
    rather than "override" — honest about what actually drove the
    result."""
    cfg = load_default_config()
    override = NicOverride(use_custom=True, ip="", subnet="", gateway=None)
    eff = effective_nic_for_role(Role.SERVER, cfg, override)
    assert eff.ip == "192.168.1.1"
    assert eff.subnet_mask == "255.255.255.0"
    assert eff.gateway is None
    assert eff.source == "profile"


def test_override_whitespace_only_field_treated_as_empty() -> None:
    """Stray whitespace from user input doesn't shadow the profile."""
    cfg = load_default_config()
    override = NicOverride(use_custom=True, ip="   ", subnet="\t", gateway=None)
    eff = effective_nic_for_role(Role.CLIENT, cfg, override)
    assert eff.ip == "192.168.1.2"
    assert eff.source == "profile"


def test_override_clears_profile_gateway_with_empty_string() -> None:
    """If the profile has a gateway but the user explicitly types empty
    string into the gateway override field, the result is no gateway —
    not the profile default. Distinct from None (no override supplied).

    This case requires a profile with a gateway set; we construct one
    by hand because defaults.json ships gateway: null.
    """
    cfg = load_default_config()
    # Mutate the profile to simulate a config that has a default gateway.
    cfg.network.gateway = "192.168.1.254"  # type: ignore[assignment]

    # 1) override.gateway = None -> profile gateway wins
    ov_none = NicOverride(use_custom=True, ip="", subnet="", gateway=None)
    assert effective_nic_for_role(Role.SERVER, cfg, ov_none).gateway == "192.168.1.254"

    # 2) override.gateway = "" -> explicitly cleared (no gateway)
    ov_empty = NicOverride(use_custom=True, ip="", subnet="", gateway="")
    assert effective_nic_for_role(Role.SERVER, cfg, ov_empty).gateway is None


# ---------------------------------------------------------------------------
# Immutability — EffectiveNic is frozen
# ---------------------------------------------------------------------------


def test_effective_nic_is_frozen() -> None:
    """Callers can hand the result around without worrying about mutation."""
    import pytest
    cfg = load_default_config()
    eff = effective_nic_for_role(Role.SERVER, cfg, None)
    with pytest.raises((AttributeError, Exception)):
        eff.ip = "10.0.0.1"  # type: ignore[misc]
