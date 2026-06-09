"""Tests for first-launch role auto-detection from local IPs.

The pure helpers in :mod:`pingpair.core.role_detect` are fully testable
without spinning up Qt or sockets — feed them a fake list of bound
IPv4 addresses and check the role mapping.

The banner-text helper :func:`evaluate_role_ip_warning` lives next door
in the same module but its tests are in ``test_role_warning.py`` to
keep this file focused on the role-mapping logic.
"""

from __future__ import annotations

from pingpair.context import Role
from pingpair.core.role_detect import (
    detect_role,
    detect_role_for_addresses,
    local_ipv4_addresses,
)

CANONICAL_SERVER = "192.168.1.1"
CANONICAL_CLIENT = "192.168.1.2"


def test_server_ip_match_picks_server_role() -> None:
    role = detect_role_for_addresses(
        [CANONICAL_SERVER, "10.0.0.5"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert role is Role.SERVER


def test_client_ip_match_picks_client_role() -> None:
    role = detect_role_for_addresses(
        [CANONICAL_CLIENT, "fe80::1"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert role is Role.CLIENT


def test_no_match_falls_back_to_client() -> None:
    """If neither canonical IP is bound, default to Client (safe fallback)."""
    role = detect_role_for_addresses(
        ["10.0.0.5", "172.16.99.99"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert role is Role.CLIENT


def test_server_takes_precedence_when_both_bound() -> None:
    """A multi-NIC PC with both 192.168.1.1 and 192.168.1.2 should be Server."""
    role = detect_role_for_addresses(
        [CANONICAL_SERVER, CANONICAL_CLIENT],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert role is Role.SERVER


def test_empty_address_list_falls_back_to_client() -> None:
    """If gethostbyname returned nothing useful (or errored), default Client."""
    role = detect_role_for_addresses(
        [],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert role is Role.CLIENT


def test_local_ipv4_addresses_returns_a_list() -> None:
    """Smoke test the live-system enumeration: shouldn't error, returns list."""
    addrs = local_ipv4_addresses()
    assert isinstance(addrs, list)
    assert all(not a.startswith("127.") for a in addrs)


# ---- detect_role: the first-launch POLICY (IP auto-detect, else Loopback) ----
# (The no-match default flipped Client -> Loopback on 2026-06-02; the pure
# detect_role_for_addresses fallback above stays Client.)


def test_detect_role_no_match_defaults_loopback() -> None:
    """A fresh PC with neither canonical IP bound opens in Loopback, not Client,
    and reports matched=False (the role came from the default, not an IP hit)."""
    role, matched = detect_role(
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
        bound_ips=["10.0.0.5", "172.16.99.99"],
    )
    assert role is Role.LOOPBACK
    assert matched is False


def test_detect_role_empty_addrs_defaults_loopback() -> None:
    """No bound IPs at all (enumeration empty/errored) -> Loopback default."""
    role, matched = detect_role(
        server_ip=CANONICAL_SERVER, client_ip=CANONICAL_CLIENT, bound_ips=[]
    )
    assert role is Role.LOOPBACK
    assert matched is False


def test_detect_role_server_ip_still_wins() -> None:
    """The IP auto-detect still takes precedence over the Loopback default."""
    role, matched = detect_role(
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
        bound_ips=[CANONICAL_SERVER, "10.0.0.5"],
    )
    assert role is Role.SERVER
    assert matched is True


def test_detect_role_client_ip_still_wins() -> None:
    role, matched = detect_role(
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
        bound_ips=[CANONICAL_CLIENT],
    )
    assert role is Role.CLIENT
    assert matched is True
