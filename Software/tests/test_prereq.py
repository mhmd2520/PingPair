"""Tests for the prereq checker.

We mock subprocess and psutil so the suite is hermetic — no real ping,
no real netsh, no real adapters.  The goal is to lock down the parsing
and aggregation logic, not the OS interactions.
"""

from __future__ import annotations

import subprocess
import sys
from collections import namedtuple
from types import SimpleNamespace
from typing import Any

import pytest

from pingpair.config import load_default_config
from pingpair.core import prereq


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Replace subprocess.run inside the prereq module with ``handler``."""
    monkeypatch.setattr(prereq.subprocess, "run", handler)


# ---------------------------------------------------------------------------
# Python version
# ---------------------------------------------------------------------------


def test_python_version_passes_when_recent_enough() -> None:
    r = prereq.check_python_version(minimum=(3, 8))
    assert r.status is prereq.Status.PASS


def test_python_version_fails_when_too_old() -> None:
    r = prereq.check_python_version(minimum=(99, 0))
    assert r.status is prereq.Status.FAIL
    assert "99.0" in r.detail


# ---------------------------------------------------------------------------
# NIC IP
# ---------------------------------------------------------------------------


_FakeAddr = namedtuple("_FakeAddr", ["family", "address"])


class _FakeFamily:
    """Mimics the ``family.name`` attribute psutil uses."""

    def __init__(self, name: str, value: int = 2) -> None:
        self.name = name
        self.value = value


def _fake_psutil(addrs: dict[str, list[_FakeAddr]]) -> Any:
    return SimpleNamespace(net_if_addrs=lambda: addrs)


def test_nic_ip_passes_when_server_ip_present(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.1")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg)
    assert r.status is prereq.Status.PASS
    assert "Server" in r.detail


def test_nic_ip_passes_when_client_ip_present(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Wi-Fi": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.2")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg)
    assert r.status is prereq.Status.PASS
    assert "Client" in r.detail


def test_nic_ip_fails_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "10.0.0.5")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg)
    assert r.status is prereq.Status.FAIL
    assert "10.0.0.5" in r.detail
    assert r.fix_action_id == "set_static_ip"


# ---------------------------------------------------------------------------
# Role-aware NIC IP check (added 2026-05-16 to fix the
# "Setup tab says PASS but orange banner says FIX ME" inconsistency)
# ---------------------------------------------------------------------------


def test_nic_ip_server_role_passes_when_server_ip_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.1")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg, prereq.Role.SERVER)
    assert r.status is prereq.Status.PASS
    assert r.detail.startswith("Server role:")
    assert "192.168.1.1" in r.detail


def test_nic_ip_server_role_fails_when_only_client_ip_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saved role=Server but the wrong canonical IP is bound -> FAIL.

    This is the exact scenario the Setup-tab orange banner already
    flags via evaluate_role_ip_warning; the prereq table must match.
    """
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet0": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.2")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg, prereq.Role.SERVER)
    assert r.status is prereq.Status.FAIL
    assert r.fix_action_id == "set_static_ip"
    assert "Server role expects 192.168.1.1" in r.detail
    # The detail names which adapter has the wrong IP so the user knows
    # what they're about to change.
    assert "Ethernet0" in r.detail
    assert "192.168.1.2" in r.detail


def test_nic_ip_client_role_passes_when_client_ip_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.2")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg, prereq.Role.CLIENT)
    assert r.status is prereq.Status.PASS
    assert r.detail.startswith("Client role:")


def test_nic_ip_client_role_fails_when_only_server_ip_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug Mohamed flagged on the VM screenshot: PC1 had role=Client
    but was still bound to 192.168.1.1, and the prereq said PASS.
    """
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet0": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.1")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg, prereq.Role.CLIENT)
    assert r.status is prereq.Status.FAIL
    assert r.fix_action_id == "set_static_ip"
    assert "Client role expects 192.168.1.2" in r.detail
    assert "Ethernet0" in r.detail
    assert "192.168.1.1" in r.detail


def test_nic_ip_loopback_role_passes_regardless_of_bound_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback dev mode skips the NIC IP requirement entirely."""
    cfg = load_default_config()
    # An arbitrary non-canonical IP — should still PASS in loopback.
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "10.0.0.5")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg, prereq.Role.LOOPBACK)
    assert r.status is prereq.Status.PASS
    assert "Loopback" in r.detail
    assert r.fix_action_id is None

def test_nic_ip_role_fail_when_neither_canonical_ip_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role=Server but neither canonical IP is bound -> FAIL with a
    detail that names the saved role's expected IP."""
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "10.0.0.5")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_nic_ip(cfg, prereq.Role.SERVER)
    assert r.status is prereq.Status.FAIL
    assert r.fix_action_id == "set_static_ip"
    assert "Server role expects 192.168.1.1" in r.detail
    # And the actual bound IP appears in the diagnostic.
    assert "10.0.0.5" in r.detail


def test_nic_ip_undecided_preserves_legacy_lenient_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The --check-prereqs CLI path runs before any role is loaded
    from QSettings, so it defaults to role=UNDECIDED. Either canonical
    IP must keep PASSing — the legacy contract for CLI users.
    """
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.1")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r1 = prereq.check_nic_ip(cfg, prereq.Role.UNDECIDED)
    r2 = prereq.check_nic_ip(cfg)
    assert r1.status is prereq.Status.PASS
    assert r2.status is prereq.Status.PASS
    assert "Server role detected" in r1.detail
    assert r1.detail == r2.detail


# ---------------------------------------------------------------------------
# Firewall rule parsing
# ---------------------------------------------------------------------------


_NETSH_PRESENT = """\
Rule Name:                            PingPair ICMP echo (in)
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Domain,Private,Public
Grouping:
LocalIP:                              Any
RemoteIP:                             Any
Protocol:                             ICMPv4
Type:                                 8
Code:                                 Any
Edge traversal:                       No
Action:                               Allow
Ok.
"""

_NETSH_ABSENT = "No rules match the specified criteria.\n"


def test_netsh_rule_exists_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(
        monkeypatch,
        lambda *_a, **_kw: _Completed(stdout=_NETSH_PRESENT),
    )
    exists, output = prereq._netsh_rule_exists("PingPair ICMP echo (in)")
    assert exists is True
    assert "Rule Name" in output


def test_netsh_rule_exists_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(
        monkeypatch,
        lambda *_a, **_kw: _Completed(stdout=_NETSH_ABSENT),
    )
    exists, output = prereq._netsh_rule_exists("Some-Missing-Rule")
    assert exists is False
    assert "No rules match" in output


def test_firewall_check_passes_when_rule_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    _patch_subprocess(
        monkeypatch,
        lambda *_a, **_kw: _Completed(stdout=_NETSH_PRESENT),
    )
    r = prereq.check_firewall_icmp()
    assert r.status is prereq.Status.PASS
    assert "present" in r.detail.lower() or "ok" in r.detail.lower() \
        or "yes" in r.detail.lower() or "icmp" in r.detail.lower()


def test_firewall_check_warns_when_rule_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    _patch_subprocess(
        monkeypatch,
        lambda *_a, **_kw: _Completed(stdout=_NETSH_ABSENT),
    )
    r = prereq.check_firewall_icmp()
    assert r.status is prereq.Status.WARN
    assert r.fix_action_id is not None


def test_firewall_check_skipped_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    r = prereq.check_firewall_icmp()
    assert r.status is prereq.Status.SKIP
    assert "linux" in r.detail.lower()


# ---------------------------------------------------------------------------
# run_checks aggregation
# ---------------------------------------------------------------------------


def test_run_checks_returns_all_registered_checks() -> None:
    """Lock in the full set of checks so adding one without updating this
    test fails loudly. Expected count tracks ``run_checks()`` exactly:

      Ethernet cable, Python, NIC IP, Wi-Fi off, Gateway reachable,
      ICMP fw, iperf3 fw, control fw  =>  8 entries.

    The fping / iperf3 binary probes and the Administrator-privileges
    check were removed: the binaries are bundled with the app and the
    GUI auto-elevates, so neither needs a prereq row.

    Order also matters - the Setup tab renders them in this order, and the
    physical Ethernet cable leads because every other check depends on it.
    """
    cfg = load_default_config()
    results = prereq.run_checks(cfg)
    names = [r.name for r in results]
    assert names == [
        "Ethernet cable",
        "Python interpreter",
        "Local NIC IP",
        "Wi-Fi disabled",
        "Gateway reachable",
        "Firewall: ICMP echo (ping)",
        "Firewall: iperf3 (TCP/UDP 5201)",
        "Firewall: control channel (TCP 5202)",
    ]


def test_has_blockers_only_counts_fail() -> None:
    results = [
        prereq.CheckResult("a", prereq.Status.PASS, ""),
        prereq.CheckResult("b", prereq.Status.WARN, ""),
        prereq.CheckResult("c", prereq.Status.SKIP, ""),
    ]
    assert prereq.has_blockers(results) is False
    results.append(prereq.CheckResult("d", prereq.Status.FAIL, ""))
    assert prereq.has_blockers(results) is True


# ---------------------------------------------------------------------------
# Override-aware NIC IP check (Group F / Q1, 2026-05-16)
# ---------------------------------------------------------------------------


def test_nic_ip_override_active_uses_override_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-PC override is applied with use_custom=True, the
    check expects the override IP — not the profile default. The detail
    string includes "(custom)" to make the source visible."""
    from pingpair.context import NicOverride
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "10.0.0.5")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    override = NicOverride(use_custom=True, ip="10.0.0.5")
    r = prereq.check_nic_ip(cfg, prereq.Role.SERVER, override)
    assert r.status is prereq.Status.PASS
    assert "(custom)" in r.detail
    assert "10.0.0.5" in r.detail


def test_nic_ip_override_inactive_falls_back_to_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override.use_custom=False means the typed values are dormant.
    The check uses the profile default — no "(custom)" tag in detail."""
    from pingpair.context import NicOverride
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.1")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    override = NicOverride(use_custom=False, ip="10.0.0.5")
    r = prereq.check_nic_ip(cfg, prereq.Role.SERVER, override)
    assert r.status is prereq.Status.PASS
    assert "(custom)" not in r.detail
    assert "192.168.1.1" in r.detail


def test_nic_ip_override_active_but_wrong_ip_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User customised the IP via Setup but the NIC isn't bound to that
    yet — should FAIL with set_static_ip fix so the user can apply the
    override via netsh."""
    from pingpair.context import NicOverride
    cfg = load_default_config()
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_FakeFamily("AF_INET"), "192.168.1.1")]}
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    override = NicOverride(use_custom=True, ip="10.0.0.5")
    r = prereq.check_nic_ip(cfg, prereq.Role.SERVER, override)
    assert r.status is prereq.Status.FAIL
    assert r.fix_action_id == "set_static_ip"
    assert "10.0.0.5" in r.detail              # the override IP
    assert "192.168.1.1" in r.detail            # what's currently bound


# ---------------------------------------------------------------------------
# Group F (Q1 task #11, 2026-05-16): Gateway reachable prereq
# ---------------------------------------------------------------------------


def test_gateway_reachable_skips_when_no_gateway_configured() -> None:
    """Canonical point-to-point LAN has no gateway -> SKIP, not WARN."""
    from pingpair.context import NicOverride
    cfg = load_default_config()  # ships gateway: null
    r = prereq.check_gateway_reachable(cfg, prereq.Role.SERVER, NicOverride())
    assert r.status is prereq.Status.SKIP
    assert "no gateway" in r.detail.lower() or "point-to-point" in r.detail.lower()


def test_gateway_reachable_skips_on_loopback_role() -> None:
    from pingpair.context import NicOverride
    cfg = load_default_config()
    r = prereq.check_gateway_reachable(cfg, prereq.Role.LOOPBACK, NicOverride())
    assert r.status is prereq.Status.SKIP


def test_gateway_reachable_skips_on_undecided_role() -> None:
    from pingpair.context import NicOverride
    cfg = load_default_config()
    r = prereq.check_gateway_reachable(cfg, prereq.Role.UNDECIDED, NicOverride())
    assert r.status is prereq.Status.SKIP


def test_gateway_reachable_skips_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = load_default_config()
    r = prereq.check_gateway_reachable(cfg, prereq.Role.SERVER)
    assert r.status is prereq.Status.SKIP


def test_gateway_reachable_passes_when_ping_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway that ping returns rc=0 for -> PASS."""
    from pingpair.context import NicOverride
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = load_default_config()
    # Force a gateway via the override path.
    override = NicOverride(use_custom=True, gateway="192.168.1.254")
    _patch_subprocess(
        monkeypatch,
        lambda *_a, **_kw: _Completed(stdout="Reply from 192.168.1.254", returncode=0),
    )
    r = prereq.check_gateway_reachable(cfg, prereq.Role.SERVER, override)
    assert r.status is prereq.Status.PASS
    assert "192.168.1.254" in r.detail


def test_gateway_reachable_warns_when_ping_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured gateway that doesn't respond is WARN, not FAIL —
    test execution still works on a point-to-point LAN that ignores
    the gateway."""
    from pingpair.context import NicOverride
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = load_default_config()
    override = NicOverride(use_custom=True, gateway="10.99.99.1")
    _patch_subprocess(
        monkeypatch,
        lambda *_a, **_kw: _Completed(
            stdout="Request timed out.", returncode=1
        ),
    )
    r = prereq.check_gateway_reachable(cfg, prereq.Role.SERVER, override)
    assert r.status is prereq.Status.WARN
    assert "10.99.99.1" in r.detail
