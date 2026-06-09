"""Tests for the dynamic ``set_static_ip`` fix.

Pure-Python coverage of the argv builder + the role/adapter resolver.
The actual ``netsh`` subprocess call is not exercised — we just lock
in the exact argv shape that ``run_fix`` will hand to subprocess.
"""

from __future__ import annotations

from collections import namedtuple
from types import SimpleNamespace

import pytest

from pingpair.config import load_default_config
from pingpair.context import AppContext, RunState, Role
from pingpair.core import fix_actions
from pingpair.core.fix_actions import (
    FIX_ACTIONS,
    FixResult,
    build_set_static_ip_argv,
    detect_primary_adapter,
    resolve_set_static_ip,
    run_fix,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FakeAddr = namedtuple("_FakeAddr", "address family")


def _ipv4(addr: str) -> _FakeAddr:
    """Mimic the shape psutil.net_if_addrs() returns for an IPv4 entry."""
    fam = SimpleNamespace(name="AF_INET", value=2)
    return _FakeAddr(address=addr, family=fam)


def _ctx_with_role(role: Role) -> AppContext:
    """Build a minimal AppContext for the resolver tests.

    Avoid :meth:`RunState.from_config` here — that path imports
    ``pingpair.settings`` which transitively pulls in PySide6.QtCore for
    QSettings. The resolver itself doesn't touch any of that, so we bypass
    the helper and construct a bare RunState manually.
    """
    cfg = load_default_config()
    rs = RunState(role=role)
    return AppContext(config=cfg, logger=__import__("logging").getLogger("test"), run_state=rs)


# ---------------------------------------------------------------------------
# build_set_static_ip_argv
# ---------------------------------------------------------------------------


def test_build_argv_matches_netsh_shape() -> None:
    argv = build_set_static_ip_argv(
        adapter="Ethernet0",
        ip="192.168.1.2",
        subnet_mask="255.255.255.0",
    )
    assert argv == [
        "netsh", "interface", "ipv4", "set", "address",
        "name=Ethernet0",
        "source=static",
        "address=192.168.1.2",
        "mask=255.255.255.0",
    ]


def test_build_argv_handles_adapter_with_spaces() -> None:
    """Names with spaces (like 'Local Area Connection') round-trip through name=."""
    argv = build_set_static_ip_argv(
        adapter="Local Area Connection",
        ip="192.168.1.1",
        subnet_mask="255.255.255.0",
    )
    assert "name=Local Area Connection" in argv


# ---------------------------------------------------------------------------
# detect_primary_adapter (mocked psutil)
# ---------------------------------------------------------------------------


def test_detect_primary_adapter_prefers_ethernet(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = {
        "Ethernet0": [_ipv4("192.168.1.2")],
        "Wi-Fi": [_ipv4("10.0.0.5")],
        "Loopback Pseudo-Interface 1": [_ipv4("127.0.0.1")],
    }
    monkeypatch.setattr(
        "psutil.net_if_addrs", lambda: fake, raising=False,
    )
    assert detect_primary_adapter() == "Ethernet0"


def test_detect_primary_adapter_skips_virtual(monkeypatch: pytest.MonkeyPatch) -> None:
    """vEthernet, vmnet, VMware, Loopback prefixes are blacklisted."""
    fake = {
        "vEthernet (Default Switch)": [_ipv4("172.20.5.1")],
        "vmnet1": [_ipv4("172.16.10.1")],
        "VMware Network Adapter VMnet8": [_ipv4("172.16.20.1")],
        "Ethernet0": [_ipv4("192.168.1.1")],
    }
    monkeypatch.setattr("psutil.net_if_addrs", lambda: fake, raising=False)
    assert detect_primary_adapter() == "Ethernet0"


def test_detect_primary_adapter_returns_none_when_only_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = {"Loopback Pseudo-Interface 1": [_ipv4("127.0.0.1")]}
    monkeypatch.setattr("psutil.net_if_addrs", lambda: fake, raising=False)
    assert detect_primary_adapter() is None


def test_detect_primary_adapter_skips_apipa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapters with only 169.254.x.x (link-local APIPA) are not real candidates."""
    fake = {
        "Ethernet0": [_ipv4("169.254.1.50")],
        "Ethernet1": [_ipv4("192.168.1.2")],
    }
    monkeypatch.setattr("psutil.net_if_addrs", lambda: fake, raising=False)
    assert detect_primary_adapter() == "Ethernet1"


# ---------------------------------------------------------------------------
# resolve_set_static_ip
# ---------------------------------------------------------------------------


def test_resolve_server_role_targets_server_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fix_actions, "detect_primary_adapter", lambda: "Ethernet0")
    ctx = _ctx_with_role(Role.SERVER)
    resolved = resolve_set_static_ip(ctx)
    assert resolved.fallback is False
    assert resolved.argv == [
        "netsh", "interface", "ipv4", "set", "address",
        "name=Ethernet0", "source=static",
        "address=192.168.1.1", "mask=255.255.255.0",
    ]
    assert "192.168.1.1" in resolved.label


def test_resolve_client_role_targets_client_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fix_actions, "detect_primary_adapter", lambda: "Ethernet0")
    ctx = _ctx_with_role(Role.CLIENT)
    resolved = resolve_set_static_ip(ctx)
    assert resolved.fallback is False
    assert "address=192.168.1.2" in resolved.argv
    assert "192.168.1.2" in resolved.label


def test_resolve_loopback_role_falls_back_to_ncpa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback has no canonical IP — open the adapter panel instead."""
    monkeypatch.setattr(fix_actions, "detect_primary_adapter", lambda: "Ethernet0")
    ctx = _ctx_with_role(Role.LOOPBACK)
    resolved = resolve_set_static_ip(ctx)
    assert resolved.fallback is True
    assert resolved.argv == ["control", "ncpa.cpl"]


def test_resolve_no_adapter_falls_back_to_ncpa(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the adapter detector returns None, the fix opens ncpa.cpl."""
    monkeypatch.setattr(fix_actions, "detect_primary_adapter", lambda: None)
    ctx = _ctx_with_role(Role.SERVER)
    resolved = resolve_set_static_ip(ctx)
    assert resolved.fallback is True
    assert resolved.argv == ["control", "ncpa.cpl"]


# ---------------------------------------------------------------------------
# FIX_ACTIONS registry sanity
# ---------------------------------------------------------------------------


def test_set_static_ip_action_label_and_admin_flag() -> None:
    """Catch the case where someone reverts the label or admin flag."""
    action = FIX_ACTIONS["set_static_ip"]
    assert action.label == "Set the correct IP"
    assert action.needs_admin is True


# ---------------------------------------------------------------------------
# 2026-05-12 follow-up: APIPA-tolerant detector + DHCP-release fallback
# ---------------------------------------------------------------------------


def test_detect_primary_adapter_accepts_apipa_only(monkeypatch) -> None:
    """An adapter with only 169.254.x.x should still be detected.

    Previously the IPv4-presence filter excluded APIPA — which meant
    the IP-fix silently fell back to opening ncpa.cpl whenever DHCP
    failed. The whole point of the fix is to recover from exactly that
    state. Regression test for #13 follow-up 2026-05-12.
    """
    import collections
    Addr = collections.namedtuple("Addr", "address family")

    fake = {
        "Ethernet0": [Addr("169.254.15.100", 2), Addr("fe80::abcd", 23)],
        "Loopback Pseudo-Interface 1": [Addr("127.0.0.1", 2)],
    }

    class _FakePsutil:
        @staticmethod
        def net_if_addrs():
            return fake

    monkeypatch.setattr(
        "pingpair.core.fix_actions.psutil", _FakePsutil, raising=False,
    )
    # detect_primary_adapter imports psutil lazily; we patch the import.
    import sys
    sys.modules.pop("psutil", None)
    sys.modules["psutil"] = _FakePsutil
    try:
        from pingpair.core.fix_actions import detect_primary_adapter
        assert detect_primary_adapter() == "Ethernet0"
    finally:
        sys.modules.pop("psutil", None)


def test_build_release_dhcp_argv_shape() -> None:
    from pingpair.core.fix_actions import build_release_dhcp_argv
    argv = build_release_dhcp_argv("Ethernet0")
    assert argv == [
        "netsh", "interface", "ipv4", "set", "address",
        "name=Ethernet0", "source=dhcp",
    ]


def test_build_set_static_ip_argv_uses_explicit_keyword_form() -> None:
    """The new keyword form is more permissive on DHCP→static transitions."""
    from pingpair.core.fix_actions import build_set_static_ip_argv
    argv = build_set_static_ip_argv(
        adapter="Ethernet0", ip="192.168.1.2", subnet_mask="255.255.255.0",
    )
    # Must use source=static / address=… / mask=… keyword form.
    assert "source=static" in argv
    assert "address=192.168.1.2" in argv
    assert "mask=255.255.255.0" in argv
    # Must NOT use the old positional shorthand (which fails on some
    # Windows builds when adapter is on DHCP).
    assert "static" not in argv  # only "source=static" should appear


# ---------------------------------------------------------------------------
# Group F (Q1, 2026-05-16): gateway support in build_set_static_ip_argv
# ---------------------------------------------------------------------------


def test_build_argv_without_gateway_omits_gateway_clause() -> None:
    """No gateway = no ``gateway=`` / ``gwmetric=`` tokens. Canonical
    point-to-point LAN setup matches the legacy argv shape."""
    from pingpair.core.fix_actions import build_set_static_ip_argv
    argv = build_set_static_ip_argv(
        adapter="Ethernet0", ip="192.168.1.1", subnet_mask="255.255.255.0",
    )
    assert not any(t.startswith("gateway=") for t in argv)
    assert not any(t.startswith("gwmetric=") for t in argv)


def test_build_argv_with_gateway_appends_gwmetric_one() -> None:
    """Non-empty gateway adds ``gateway=<gw> gwmetric=1`` to the argv.
    gwmetric=1 keeps the test NIC from outranking the user's normal
    default route."""
    from pingpair.core.fix_actions import build_set_static_ip_argv
    argv = build_set_static_ip_argv(
        adapter="Ethernet0",
        ip="10.0.0.5",
        subnet_mask="255.255.0.0",
        gateway="10.0.0.1",
    )
    assert "gateway=10.0.0.1" in argv
    assert "gwmetric=1" in argv
    # Gateway tokens come AFTER the address/mask keywords.
    gw_idx = argv.index("gateway=10.0.0.1")
    addr_idx = argv.index("address=10.0.0.5")
    assert gw_idx > addr_idx


def test_build_argv_with_empty_gateway_omits_gateway_clause() -> None:
    """gateway=None and gateway='' both mean 'no gateway'."""
    from pingpair.core.fix_actions import build_set_static_ip_argv
    argv1 = build_set_static_ip_argv(
        adapter="Eth0", ip="10.0.0.1", subnet_mask="255.0.0.0", gateway=None,
    )
    argv2 = build_set_static_ip_argv(
        adapter="Eth0", ip="10.0.0.1", subnet_mask="255.0.0.0", gateway="",
    )
    assert not any(t.startswith("gateway=") for t in argv1)
    assert not any(t.startswith("gateway=") for t in argv2)


# ---------------------------------------------------------------------------
# run_fix dispatch: registry KEY vs ResolvedFix.label
# ---------------------------------------------------------------------------


def _ok_run_argv(*_a, **_k) -> FixResult:
    return FixResult(ok=True, stdout="", stderr="", returncode=0)


def test_run_fix_needs_registry_key_not_resolved_label(monkeypatch) -> None:
    """Regression: the external-IP 'Restore previous' path must call run_fix
    with the registry KEY 'set_static_ip', NOT a ResolvedFix.label. Passing a
    label (e.g. 'Set IP to 192.168.1.2') KeyError'd in FIX_ACTIONS[...] so the
    restore silently never ran (setup_view._check_external_ip_change)."""
    monkeypatch.setattr(fix_actions, "detect_primary_adapter", lambda: "Ethernet0")
    monkeypatch.setattr(fix_actions, "_run_argv", _ok_run_argv)

    ctx = _ctx_with_role(Role.CLIENT)
    resolved = resolve_set_static_ip(ctx)
    # The human-facing label is NOT a valid fix_id.
    assert resolved.label not in FIX_ACTIONS
    with pytest.raises(KeyError):
        run_fix(resolved.label, ctx=ctx)
    # The registry key is what works.
    assert run_fix("set_static_ip", ctx=ctx).ok


# ---------------------------------------------------------------------------
# Don't configure a DISCONNECTED Ethernet adapter (cable unplugged)
# ---------------------------------------------------------------------------


def test_adapter_link_up_reflects_isup(monkeypatch) -> None:
    monkeypatch.setattr(
        "psutil.net_if_stats",
        lambda: {
            "Ethernet0": SimpleNamespace(isup=True),
            "Eth-down": SimpleNamespace(isup=False),
        },
        raising=False,
    )
    assert fix_actions.adapter_link_up("Ethernet0") is True
    assert fix_actions.adapter_link_up("Eth-down") is False
    # Unknown adapter -> conservative True (don't block on uncertainty).
    assert fix_actions.adapter_link_up("Nope") is True


def test_run_fix_refuses_static_ip_on_disconnected_adapter(monkeypatch) -> None:
    """Cable unplugged -> refuse, and NEVER run netsh. Windows would silently
    drop a static IP to 'media disconnected', so the NIC looks configured but
    is unusable (matches the Ethernet-cable prereq FAIL)."""
    monkeypatch.setattr(fix_actions, "detect_primary_adapter", lambda: "Ethernet0")
    monkeypatch.setattr(fix_actions, "adapter_link_up", lambda _a: False)
    ran = {"netsh": False}

    def _spy(*_a, **_k) -> FixResult:
        ran["netsh"] = True
        return FixResult(ok=True, stdout="", stderr="", returncode=0)

    monkeypatch.setattr(fix_actions, "_run_argv", _spy)

    ctx = _ctx_with_role(Role.CLIENT)
    result = run_fix("set_static_ip", ctx=ctx)
    assert not result.ok
    assert "unplugged" in result.stderr.lower()
    assert ran["netsh"] is False  # refused BEFORE touching netsh


def test_run_fix_sets_ip_when_link_up(monkeypatch) -> None:
    """Cable connected (incl. the APIPA / DHCP-failed case) -> set proceeds."""
    monkeypatch.setattr(fix_actions, "detect_primary_adapter", lambda: "Ethernet0")
    monkeypatch.setattr(fix_actions, "adapter_link_up", lambda _a: True)
    monkeypatch.setattr(fix_actions, "_run_argv", _ok_run_argv)

    ctx = _ctx_with_role(Role.CLIENT)
    assert run_fix("set_static_ip", ctx=ctx).ok
