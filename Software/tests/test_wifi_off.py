"""Tests for the Wi-Fi-off prereq check + dynamic disable_wifi fix.

Pure-Python coverage. We mock ``psutil.net_if_addrs`` so the test box's
real network state is irrelevant. The actual ``netsh`` subprocess call
is not exercised — we just lock in the argv shape and the resolver
fallback behaviour.
"""

from __future__ import annotations

import sys
from collections import namedtuple
from types import SimpleNamespace

import pytest

from pingpair.core import fix_actions
from pingpair.core.fix_actions import (
    FIX_ACTIONS,
    build_disable_wifi_argv,
    detect_wifi_adapter,
    resolve_disable_wifi,
)
from pingpair.core.prereq import (
    Status,
    _wifi_adapters_with_ipv4,
    check_wifi_off,
    wifi_adapters_on_test_subnet,
    wifi_on_test_subnet,
)


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_fix_static_ip.py for consistency)
# ---------------------------------------------------------------------------


_FakeAddr = namedtuple("_FakeAddr", "address family")


def _ipv4(addr: str) -> _FakeAddr:
    fam = SimpleNamespace(name="AF_INET", value=2)
    return _FakeAddr(address=addr, family=fam)


def _mock_adapters_up(monkeypatch: pytest.MonkeyPatch, addrs: dict) -> None:
    """Mock net_if_addrs AND net_if_stats (all adapters up) together.

    ``_wifi_adapters_with_ipv4`` skips *down* adapters via net_if_stats, so a
    test that mocks only net_if_addrs lets the REAL machine's same-named
    adapter (e.g. a "Wi-Fi" NIC the operator disabled during testing) decide
    the result — a hidden environment dependency. Mocking both keeps the test
    hermetic regardless of the box's live network state.
    """
    monkeypatch.setattr("psutil.net_if_addrs", lambda: addrs, raising=False)
    monkeypatch.setattr(
        "psutil.net_if_stats",
        lambda: {name: SimpleNamespace(isup=True) for name in addrs},
        raising=False,
    )


def _force_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we're on Windows for the duration of one test.

    The check_wifi_off() short-circuits to SKIP on non-Windows, which
    is right behaviour but breaks the assertions we want to write here.
    """
    monkeypatch.setattr(sys, "platform", "win32")


# ---------------------------------------------------------------------------
# _wifi_adapters_with_ipv4 (pure helper)
# ---------------------------------------------------------------------------


def test_wifi_helper_finds_named_wifi_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = {
        "Ethernet0": [_ipv4("192.168.1.2")],
        "Wi-Fi": [_ipv4("10.0.0.5")],
    }
    _mock_adapters_up(monkeypatch, fake)
    found = _wifi_adapters_with_ipv4()
    assert found == [("Wi-Fi", "10.0.0.5")]


def test_wifi_helper_matches_alternate_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names like 'Wireless Network Connection' and 'WLAN' also count."""
    fake = {
        "Wireless Network Connection": [_ipv4("10.1.1.20")],
        "WLAN-Backup": [_ipv4("10.1.1.30")],
    }
    _mock_adapters_up(monkeypatch, fake)
    names = {n for n, _ in _wifi_adapters_with_ipv4()}
    assert names == {"Wireless Network Connection", "WLAN-Backup"}


def test_wifi_helper_ignores_loopback_and_apipa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Wi-Fi NIC with only loopback / link-local IPs isn't really 'on'."""
    fake = {
        "Wi-Fi": [_ipv4("169.254.7.5"), _ipv4("127.0.0.1")],
    }
    monkeypatch.setattr("psutil.net_if_addrs", lambda: fake, raising=False)
    assert _wifi_adapters_with_ipv4() == []


def test_wifi_helper_returns_empty_when_no_wifi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = {"Ethernet0": [_ipv4("192.168.1.2")]}
    monkeypatch.setattr("psutil.net_if_addrs", lambda: fake, raising=False)
    assert _wifi_adapters_with_ipv4() == []


def test_wifi_helper_swallows_net_if_addrs_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """net_if_addrs() can raise OSError on odd NICs — the helper must return []
    (not propagate), so the Run-tab Wi-Fi hard block (an uncaught Qt slot) can't
    wedge the Run button."""
    def _boom() -> dict:
        raise OSError("odd NIC")

    monkeypatch.setattr("psutil.net_if_stats", lambda: {}, raising=False)
    monkeypatch.setattr("psutil.net_if_addrs", _boom, raising=False)
    assert _wifi_adapters_with_ipv4() == []


# ---------------------------------------------------------------------------
# check_wifi_off
# ---------------------------------------------------------------------------


def test_check_wifi_off_passes_when_wifi_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_win32(monkeypatch)
    monkeypatch.setattr(
        "psutil.net_if_addrs",
        lambda: {"Ethernet0": [_ipv4("192.168.1.1")]},
        raising=False,
    )
    result = check_wifi_off()
    assert result.status is Status.PASS
    assert result.fix_action_id is None
    # Compare both sides in lowercase — the actual detail message
    # capitalises "Wi-Fi" but we want the assertion to be case-tolerant.
    assert "no wi-fi adapter" in result.detail.lower()


def test_check_wifi_off_warns_when_wifi_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_win32(monkeypatch)
    _mock_adapters_up(
        monkeypatch,
        {
            "Ethernet0": [_ipv4("192.168.1.2")],
            "Wi-Fi": [_ipv4("10.0.0.5")],
        },
    )
    result = check_wifi_off()
    assert result.status is Status.WARN
    assert result.fix_action_id == "disable_wifi"
    # The detail should mention the offending adapter + IP so the user
    # can spot whether it's the right NIC before clicking Fix.
    assert "Wi-Fi" in result.detail
    assert "10.0.0.5" in result.detail


def test_check_wifi_off_skips_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    result = check_wifi_off()
    assert result.status is Status.SKIP
    assert "linux" in result.detail.lower()


# ---------------------------------------------------------------------------
# wifi_on_test_subnet (pure) + wifi_adapters_on_test_subnet (live wrapper)
#
# These back the Run-tab HARD BLOCK: a sweep refuses to start when a Wi-Fi
# NIC sits on the same subnet as the test link (it would steal the traffic).
# Distinct from check_wifi_off's advisory WARN, which fires for ANY live
# Wi-Fi NIC regardless of subnet.
# ---------------------------------------------------------------------------


def test_wifi_on_test_subnet_flags_same_subnet() -> None:
    """A Wi-Fi NIC inside the 192.168.1.0/24 test subnet is a conflict."""
    hits = wifi_on_test_subnet(
        "192.168.1.1",
        "255.255.255.0",
        [("Wi-Fi", "192.168.1.50")],
    )
    assert hits == [("Wi-Fi", "192.168.1.50")]


def test_wifi_on_test_subnet_ignores_different_subnet() -> None:
    """Ordinary home Wi-Fi on another subnet can't steal the test traffic."""
    hits = wifi_on_test_subnet(
        "192.168.1.1",
        "255.255.255.0",
        [("Wi-Fi", "192.168.0.50"), ("Wireless", "10.0.0.5")],
    )
    assert hits == []


def test_wifi_on_test_subnet_only_returns_offenders() -> None:
    """Mixed list → only the same-subnet adapters come back."""
    hits = wifi_on_test_subnet(
        "192.168.1.1",
        "255.255.255.0",
        [("Wi-Fi", "192.168.1.77"), ("WLAN-2", "192.168.2.77")],
    )
    assert hits == [("Wi-Fi", "192.168.1.77")]


def test_wifi_on_test_subnet_bad_input_is_no_conflict() -> None:
    """An unparseable mask / IP must never wedge the Run tab — treat as safe."""
    assert wifi_on_test_subnet("not-an-ip", "255.255.255.0", [("Wi-Fi", "192.168.1.5")]) == []
    assert wifi_on_test_subnet("192.168.1.1", "garbage", [("Wi-Fi", "192.168.1.5")]) == []
    # A bad adapter IP is skipped, not fatal.
    assert wifi_on_test_subnet(
        "192.168.1.1", "255.255.255.0", [("Wi-Fi", "bogus")]
    ) == []


def _cfg(server_ip: str = "192.168.1.1", subnet_mask: str = "255.255.255.0"):
    return SimpleNamespace(
        network=SimpleNamespace(server_ip=server_ip, subnet_mask=subnet_mask)
    )


def test_wifi_adapters_on_test_subnet_skips_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    # Even with a same-subnet Wi-Fi NIC present, non-Windows returns [] (the
    # netsh-based disable fix is Windows-only, so the block would be moot).
    _mock_adapters_up(monkeypatch, {"Wi-Fi": [_ipv4("192.168.1.50")]})
    assert wifi_adapters_on_test_subnet(_cfg()) == []


def test_wifi_adapters_on_test_subnet_blocks_same_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_win32(monkeypatch)
    _mock_adapters_up(
        monkeypatch,
        {
            "Ethernet0": [_ipv4("192.168.1.2")],
            "Wi-Fi": [_ipv4("192.168.1.50")],
        },
    )
    assert wifi_adapters_on_test_subnet(_cfg()) == [("Wi-Fi", "192.168.1.50")]


def test_wifi_adapters_on_test_subnet_allows_different_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Wi-Fi NIC on a different subnet is NOT a hard block (only a WARN)."""
    _force_win32(monkeypatch)
    _mock_adapters_up(
        monkeypatch,
        {
            "Ethernet0": [_ipv4("192.168.1.2")],
            "Wi-Fi": [_ipv4("192.168.0.50")],
        },
    )
    assert wifi_adapters_on_test_subnet(_cfg()) == []


# ---------------------------------------------------------------------------
# detect_wifi_adapter + build_disable_wifi_argv (pure helpers)
# ---------------------------------------------------------------------------


def test_detect_wifi_adapter_returns_first_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = {
        "Ethernet0": [_ipv4("192.168.1.2")],
        "Wi-Fi": [_ipv4("10.0.0.5")],
    }
    monkeypatch.setattr("psutil.net_if_addrs", lambda: fake, raising=False)
    assert detect_wifi_adapter() == "Wi-Fi"


def test_detect_wifi_adapter_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "psutil.net_if_addrs",
        lambda: {"Ethernet0": [_ipv4("192.168.1.2")]},
        raising=False,
    )
    assert detect_wifi_adapter() is None


def test_build_disable_wifi_argv_matches_netsh_shape() -> None:
    # Takes the adapter offline (admin=disabled) — reliable on any NIC and
    # easy to reverse; Reset / app-close re-enable it and Windows reconnects.
    argv = build_disable_wifi_argv("Wi-Fi")
    assert argv == [
        "netsh", "interface", "set", "interface", "name=Wi-Fi", "admin=disabled",
    ]


def test_build_disable_wifi_argv_handles_spaces_in_name() -> None:
    argv = build_disable_wifi_argv("Wireless Network Connection")
    assert "name=Wireless Network Connection" in argv


# ---------------------------------------------------------------------------
# resolve_disable_wifi
# ---------------------------------------------------------------------------


def test_resolve_disable_wifi_uses_detected_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fix_actions, "detect_wifi_adapter", lambda: "Wi-Fi")
    resolved = resolve_disable_wifi(None)
    assert resolved.fallback is False
    assert resolved.argv == [
        "netsh", "interface", "set", "interface", "name=Wi-Fi", "admin=disabled",
    ]
    assert "Wi-Fi" in resolved.label


def test_resolve_disable_wifi_falls_back_to_ncpa_when_undetected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fix_actions, "detect_wifi_adapter", lambda: None)
    resolved = resolve_disable_wifi(None)
    assert resolved.fallback is True
    assert resolved.argv == ["control", "ncpa.cpl"]


# ---------------------------------------------------------------------------
# FIX_ACTIONS registry sanity
# ---------------------------------------------------------------------------


def test_disable_wifi_action_registered_with_admin_flag() -> None:
    """Catch any future revert that drops the disable_wifi entry or its admin flag."""
    action = FIX_ACTIONS["disable_wifi"]
    assert action.label == "Disconnect Wi-Fi"
    assert action.needs_admin is True
