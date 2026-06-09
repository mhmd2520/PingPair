"""Tests for the physical-Ethernet-cable prereq (``check_ethernet_cable``).

The whole point-to-point test rides a cable between the two laptops, so the
cable check runs FIRST and FAILs loudly when there's no link — instead of
letting the IP / firewall checks fail in confusing secondary ways.
"""
from __future__ import annotations

import sys
from collections import namedtuple
from types import SimpleNamespace

import pytest

from pingpair.config import load_default_config
from pingpair.core import prereq

_FakeAddr = namedtuple("_FakeAddr", ["family", "address"])
_FakeStat = namedtuple("_FakeStat", ["isup"])


class _Family:
    def __init__(self, name: str, value: int = 2) -> None:
        self.name = name
        self.value = value


def _fake_psutil(addrs: dict, stats: dict) -> SimpleNamespace:
    return SimpleNamespace(
        net_if_addrs=lambda: addrs,
        net_if_stats=lambda: stats,
    )


def test_cable_skips_in_loopback() -> None:
    r = prereq.check_ethernet_cable(prereq.Role.LOOPBACK)
    assert r.status is prereq.Status.SKIP
    assert r.fix_action_id is None


def test_cable_passes_when_link_up(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_Family("AF_INET"), "192.168.1.1")]},
        {"Ethernet": _FakeStat(isup=True)},
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_ethernet_cable(prereq.Role.SERVER)
    assert r.status is prereq.Status.PASS
    assert "Ethernet" in r.detail


def test_cable_fails_when_link_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cable-out adapter still appears (MAC + APIPA) but isup is False -> FAIL."""
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_Family("AF_INET"), "169.254.9.9")]},
        {"Ethernet": _FakeStat(isup=False)},
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_ethernet_cable(prereq.Role.CLIENT)
    assert r.status is prereq.Status.FAIL
    assert "unplugged" in r.detail.lower()
    # A physical cable can't be fixed by software.
    assert r.fix_action_id is None


def test_cable_fails_when_no_wired_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only Wi-Fi / virtual adapters present -> no wired NIC -> FAIL."""
    fake = _fake_psutil(
        {"Wi-Fi": [_FakeAddr(_Family("AF_INET"), "10.0.0.5")]},
        {"Wi-Fi": _FakeStat(isup=True)},
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    r = prereq.check_ethernet_cable(prereq.Role.SERVER)
    assert r.status is prereq.Status.FAIL
    assert "No wired Ethernet adapter" in r.detail


def test_cable_check_runs_first_in_run_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_checks must lead with the cable row (before anything else)."""
    fake = _fake_psutil(
        {"Ethernet": [_FakeAddr(_Family("AF_INET"), "192.168.1.1")]},
        {"Ethernet": _FakeStat(isup=True)},
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    cfg = load_default_config()
    results = prereq.run_checks(cfg, prereq.Role.SERVER)
    assert results[0].name == "Ethernet cable"
