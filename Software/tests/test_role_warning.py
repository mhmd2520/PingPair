"""Tests for evaluate_role_ip_warning.

Drives the Setup-tab orange banner. Re-runs on every prereq refresh
so the banner clears once the user fixes the NIC IP via the
"Set the correct IP" button.
"""

from __future__ import annotations

from pingpair.context import Role
from pingpair.core.role_detect import evaluate_role_ip_warning

CANONICAL_SERVER = "192.168.1.1"
CANONICAL_CLIENT = "192.168.1.2"


def test_warning_empty_when_server_role_and_server_ip_bound() -> None:
    msg = evaluate_role_ip_warning(
        role=Role.SERVER,
        bound_ips=[CANONICAL_SERVER, "169.254.1.2"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert msg == ""


def test_warning_empty_when_client_role_and_client_ip_bound() -> None:
    msg = evaluate_role_ip_warning(
        role=Role.CLIENT,
        bound_ips=[CANONICAL_CLIENT],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert msg == ""


def test_warning_set_when_server_role_but_server_ip_missing() -> None:
    msg = evaluate_role_ip_warning(
        role=Role.SERVER,
        bound_ips=["192.168.10.50"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert "Server role" in msg
    assert CANONICAL_SERVER in msg
    assert "192.168.10.50" in msg


def test_warning_set_when_client_role_but_client_ip_missing() -> None:
    msg = evaluate_role_ip_warning(
        role=Role.CLIENT,
        bound_ips=["10.0.0.5"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert "Client role" in msg
    assert CANONICAL_CLIENT in msg
    assert "10.0.0.5" in msg


def test_warning_empty_when_role_is_loopback() -> None:
    msg = evaluate_role_ip_warning(
        role=Role.LOOPBACK,
        bound_ips=["192.168.10.50"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert msg == ""


def test_warning_empty_when_role_is_undecided() -> None:
    msg = evaluate_role_ip_warning(
        role=Role.UNDECIDED,
        bound_ips=[],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert msg == ""


def test_warning_clears_after_ip_fix_simulation() -> None:
    """The bug the user reported: fix the IP -> banner should disappear.

    Sequence: launch with 192.168.10.50 bound (warns), user clicks
    "Set the correct IP" so 192.168.1.2 is now bound, prereq table
    re-runs, helper called again with the new bound list.
    """
    before = evaluate_role_ip_warning(
        role=Role.CLIENT,
        bound_ips=["192.168.10.50"],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    after = evaluate_role_ip_warning(
        role=Role.CLIENT,
        bound_ips=[CANONICAL_CLIENT],
        server_ip=CANONICAL_SERVER,
        client_ip=CANONICAL_CLIENT,
    )
    assert before
    assert after == ""


# ---------------------------------------------------------------------------
# Group F (Q1 task #23, 2026-05-16): External IP-change detection
# ---------------------------------------------------------------------------

from pingpair.core.role_detect import detect_external_ip_change


def test_external_change_returns_none_for_loopback() -> None:
    assert detect_external_ip_change(
        Role.LOOPBACK, "127.0.0.1", ["127.0.0.1"], "127.0.0.1"
    ) is None


def test_external_change_returns_none_for_undecided() -> None:
    assert detect_external_ip_change(
        Role.UNDECIDED, "192.168.1.1", ["10.0.0.5"], "192.168.1.1"
    ) is None


def test_external_change_returns_none_when_no_baseline() -> None:
    """Without a last_applied baseline we don't know what 'changed' means."""
    assert detect_external_ip_change(
        Role.SERVER, "192.168.1.1", ["10.0.0.5"], last_applied=None
    ) is None
    assert detect_external_ip_change(
        Role.SERVER, "192.168.1.1", ["10.0.0.5"], last_applied=""
    ) is None


def test_external_change_returns_none_when_expected_still_bound() -> None:
    """If the expected IP is still on the wire, nothing changed externally."""
    assert detect_external_ip_change(
        Role.SERVER, "192.168.1.1",
        ["192.168.1.1", "169.254.7.5"],
        "192.168.1.1",
    ) is None


def test_external_change_returns_none_when_last_applied_still_bound() -> None:
    """Override changed but the previous IP is still bound — no divergence."""
    assert detect_external_ip_change(
        Role.SERVER, "10.0.0.1",
        ["192.168.1.1"],  # previous Setup-tab apply
        "192.168.1.1",
    ) is None


def test_external_change_detects_dhcp_reclaim() -> None:
    """Previous IP gone, replaced by a stranger -> dialog should fire."""
    result = detect_external_ip_change(
        Role.SERVER, "192.168.1.1",
        ["10.0.0.5"],            # DHCP server gave us a new IP
        "192.168.1.1",
    )
    assert result == ("192.168.1.1", "10.0.0.5")


def test_external_change_ignores_apipa_and_loopback() -> None:
    """Link-local / loopback don't count as a real "current" IP."""
    # Bound only to APIPA + loopback after the static IP got lost.
    result = detect_external_ip_change(
        Role.CLIENT, "192.168.1.2",
        ["127.0.0.1", "169.254.42.42"],
        "192.168.1.2",
    )
    # No valid current IP to report -> None.
    assert result is None
