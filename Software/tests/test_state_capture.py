"""Host network-state capture for the Setup-tab factory reset.

Covers the Qt-free, locale-sensitive core of core/state_capture.py:
the netsh-output parsers, the argv builders, and the factory-reset
diff that decides what the Setup-tab **Reset all settings** button
runs. The actual netsh execution + the GUI dialog are verified by
manual VM testing.
"""

from pingpair.core.fix_actions import build_release_dhcp_argv
from pingpair.core.state_capture import (
    NetworkSnapshot,
    build_delete_firewall_argv,
    _build_enable_wifi_argv,
    _build_reconnect_wifi_argv,
    _parse_ethernet_config,
    _parse_firewall_dump,
    _parse_first_wlan_profile,
    _parse_wifi_interface,
    compute_factory_reset_items,
    plan_close_ethernet_revert,
)


def _snap(**overrides) -> NetworkSnapshot:
    """A fully-populated snapshot; override individual fields per test."""
    base = dict(
        ethernet_adapter="Ethernet",
        ethernet_dhcp=True,
        ethernet_ip="10.0.0.5",
        ethernet_mask="255.255.255.0",
        ethernet_gateway="10.0.0.1",
        wifi_adapter="Wi-Fi",
        wifi_connected=True,
        wifi_admin_disabled=False,
        wifi_profile="HomeNet",
        firewall_preexisting={
            "icmp": False, "iperf3_tcp": False,
            "iperf3_udp": False, "control": False,
        },
    )
    base.update(overrides)
    return NetworkSnapshot(**base)


# ----- netsh parsers ---------------------------------------------------

_ETH_DHCP = """Configuration for interface "Ethernet"
    DHCP enabled:                         Yes
    IP Address:                           10.0.0.5
"""

_ETH_STATIC = """Configuration for interface "Ethernet"
    DHCP enabled:                         No
    IP Address:                           192.168.1.2
    Subnet Prefix:                        192.168.1.0/24 (mask 255.255.255.0)
    Default Gateway:                      192.168.1.1
"""


def test_parse_ethernet_dhcp() -> None:
    dhcp, gateway = _parse_ethernet_config(_ETH_DHCP)
    assert dhcp is True
    assert gateway is None


def test_parse_ethernet_static_with_gateway() -> None:
    dhcp, gateway = _parse_ethernet_config(_ETH_STATIC)
    assert dhcp is False
    assert gateway == "192.168.1.1"


def test_parse_ethernet_unreadable_is_unknown() -> None:
    dhcp, gateway = _parse_ethernet_config("netsh output in some other locale")
    assert dhcp is None
    assert gateway is None


_WLAN_PROFILES = """
Profiles on interface Wi-Fi:

Group policy profiles (read only)
---------------------------------
    <None>

User profiles
-------------
    All User Profile     : HomeNet
    All User Profile     : CafeWiFi
"""


_IFACE_TABLE = """
Admin State    State          Type             Interface Name
-------------------------------------------------------------------------
Enabled        Connected      Dedicated        Ethernet
Disabled       Disconnected   Dedicated        Wi-Fi
"""


def test_parse_wifi_interface_finds_disabled_adapter() -> None:
    # netsh lists the disabled Wi-Fi (psutil wouldn't) so reset can re-enable.
    name, disabled = _parse_wifi_interface(_IFACE_TABLE)
    assert name == "Wi-Fi"
    assert disabled is True


def test_parse_wifi_interface_none_when_no_wifi() -> None:
    out = (
        "Admin State    State          Type             Interface Name\n"
        "Enabled        Connected      Dedicated        Ethernet\n"
    )
    assert _parse_wifi_interface(out) == (None, None)


def test_parse_first_wlan_profile() -> None:
    assert _parse_first_wlan_profile(_WLAN_PROFILES) == "HomeNet"
    assert _parse_first_wlan_profile("no profiles here") is None
    # The "<None>" group-policy placeholder must be skipped.
    assert _parse_first_wlan_profile(
        "    All User Profile     : <None>\n"
        "    All User Profile     : RealNet\n"
    ) == "RealNet"


_FW_DUMP = """
Rule Name:                            PingPair ICMP echo (in)
----------------------------------------------------------------------
Enabled:                              Yes
"""


def test_parse_firewall_dump() -> None:
    assert _parse_firewall_dump(_FW_DUMP, "PingPair ICMP echo (in)") is True
    assert _parse_firewall_dump(_FW_DUMP, "PingPair control TCP 5202 (in)") is False
    assert _parse_firewall_dump("", "anything") is None
    assert _parse_firewall_dump("locale gibberish, no markers", "x") is None


def test_parse_firewall_dump_no_substring_collision() -> None:
    # A longer customer rule that merely *contains* our name must not
    # false-match — that would wrongly mark our rule as pre-existing.
    dump = (
        "Rule Name:                            "
        "PingPair ICMP echo (in) - customer copy\n"
        "Enabled:                              Yes\n"
    )
    assert _parse_firewall_dump(dump, "PingPair ICMP echo (in)") is False


# ----- factory-reset diff ----------------------------------------------


def test_factory_reset_clean_state_yields_nothing() -> None:
    # Adapter on DHCP, Wi-Fi already enabled, no PingPair firewall
    # rules present → fresh install equivalent, nothing to do.
    snap = _snap(
        ethernet_dhcp=True,
        wifi_connected=True,
        firewall_preexisting={
            "icmp": False, "iperf3_tcp": False,
            "iperf3_udp": False, "control": False,
        },
    )
    assert compute_factory_reset_items(snap) == []


def test_factory_reset_static_ip_offers_dhcp_revert() -> None:
    snap = _snap(ethernet_dhcp=False, ethernet_ip="192.168.1.2")
    items = [i for i in compute_factory_reset_items(snap) if i.kind == "ethernet"]
    assert len(items) == 1
    assert items[0].commands[0] == build_release_dhcp_argv("Ethernet")


def test_factory_reset_unknown_dhcp_offers_revert() -> None:
    # Unknown DHCP state (locale we couldn't parse) is conservatively
    # treated as "not DHCP" — running netsh set source=dhcp is harmless.
    snap = _snap(ethernet_dhcp=None, ethernet_ip="10.0.0.5")
    items = [i for i in compute_factory_reset_items(snap) if i.kind == "ethernet"]
    assert len(items) == 1


def test_factory_reset_disconnected_wifi_reenables_and_reconnects() -> None:
    # Wi-Fi off for the test (no IPv4) + a known profile → reset re-enables
    # the disabled adapter and reconnects (Windows would auto-connect anyway,
    # since the profile is left on auto-connect).
    snap = _snap(wifi_connected=False, wifi_profile="HomeNet")
    items = [i for i in compute_factory_reset_items(snap) if i.kind == "wifi"]
    assert len(items) == 1
    assert items[0].commands == [
        ["netsh", "interface", "set", "interface", "name=Wi-Fi", "admin=enabled"],
        ["netsh", "wlan", "connect", "name=HomeNet", "interface=Wi-Fi"],
    ]


def test_factory_reset_admin_disabled_wifi_is_re_enabled() -> None:
    # The test disabled the adapter (fallback path) → netsh reports it
    # disabled even though psutil can't see it; reset must re-enable it.
    snap = _snap(wifi_admin_disabled=True, wifi_connected=None, wifi_profile="HomeNet")
    items = [i for i in compute_factory_reset_items(snap) if i.kind == "wifi"]
    assert len(items) == 1
    assert items[0].commands[0] == [
        "netsh", "interface", "set", "interface", "name=Wi-Fi", "admin=enabled",
    ]


def test_factory_reset_disconnected_wifi_without_profile_reenables_only() -> None:
    # No saved profile → still re-enable the adapter (the fix may have
    # disabled a virtual "Wi-Fi" NIC), just don't guess at a reconnect.
    snap = _snap(wifi_connected=False, wifi_profile=None)
    items = [i for i in compute_factory_reset_items(snap) if i.kind == "wifi"]
    assert len(items) == 1
    assert items[0].commands == [
        ["netsh", "interface", "set", "interface", "name=Wi-Fi", "admin=enabled"],
    ]


def test_factory_reset_lists_only_present_firewall_rules() -> None:
    # icmp + control are present, iperf3 rules aren't. Factory reset
    # should propose deleting only the two that actually exist, so the
    # transcript doesn't fill with "No rules match" noise.
    snap = _snap(firewall_preexisting={
        "icmp": True, "iperf3_tcp": False,
        "iperf3_udp": False, "control": True,
    })
    fw = [i for i in compute_factory_reset_items(snap) if i.kind == "firewall"]
    assert len(fw) == 1
    assert len(fw[0].commands) == 2


def test_factory_reset_ignores_who_added_firewall_rules() -> None:
    # Factory-reset doesn't care whether the rule pre-existed PingPair's
    # launch — if it's there now and it's one of ours, it goes.
    snap = _snap(firewall_preexisting={
        "icmp": True, "iperf3_tcp": True,
        "iperf3_udp": True, "control": True,
    })
    fw = [i for i in compute_factory_reset_items(snap) if i.kind == "firewall"]
    assert len(fw) == 1
    assert len(fw[0].commands) == 4


def test_factory_reset_skips_unknown_adapter() -> None:
    # No primary Ethernet detected → no Ethernet item (never guess).
    snap = _snap(ethernet_adapter=None, ethernet_dhcp=False)
    assert not [
        i for i in compute_factory_reset_items(snap) if i.kind == "ethernet"
    ]


# ----- argv builders ---------------------------------------------------


def test_build_reconnect_wifi_argv() -> None:
    assert _build_reconnect_wifi_argv("Wi-Fi", "HomeNet") == [
        "netsh", "wlan", "connect", "name=HomeNet", "interface=Wi-Fi",
    ]


def test_build_enable_wifi_argv() -> None:
    assert _build_enable_wifi_argv("Wi-Fi") == [
        "netsh", "interface", "set", "interface", "name=Wi-Fi", "admin=enabled",
    ]


def testbuild_delete_firewall_argv() -> None:
    argv = build_delete_firewall_argv("PingPair ICMP echo (in)")
    assert argv[:5] == ["netsh", "advfirewall", "firewall", "delete", "rule"]
    assert argv[5] == "name=PingPair ICMP echo (in)"


# ----- plan_close_ethernet_revert (the X-button close gate) -----
# The close handler reverts the primary Ethernet to DHCP ONLY when it's on the
# test subnet — same DHCP-release command Reset uses, but gated so a silent
# close can't clobber an unrelated static / a Loopback-dev box (CLAUDE.md §2).


def test_close_revert_fires_when_on_test_subnet() -> None:
    # Client rig: Ethernet on 192.168.1.2, test subnet 192.168.1.0/24 -> revert.
    cmds = plan_close_ethernet_revert(
        "Ethernet", "192.168.1.2", "192.168.1.1", "255.255.255.0"
    )
    assert cmds == [build_release_dhcp_argv("Ethernet")]


def test_close_revert_server_ip_also_fires() -> None:
    cmds = plan_close_ethernet_revert(
        "Ethernet", "192.168.1.1", "192.168.1.1", "255.255.255.0"
    )
    assert cmds == [build_release_dhcp_argv("Ethernet")]


def test_close_revert_skips_unrelated_static() -> None:
    # A static on a DIFFERENT subnet (the user's own) is never clobbered.
    assert plan_close_ethernet_revert(
        "Ethernet", "10.20.30.40", "192.168.1.1", "255.255.255.0"
    ) == []


def test_close_revert_skips_apipa_and_missing() -> None:
    # APIPA (not on test subnet) and "no adapter / no IP" -> nothing to do.
    assert plan_close_ethernet_revert(
        "Ethernet", "169.254.5.5", "192.168.1.1", "255.255.255.0"
    ) == []
    assert plan_close_ethernet_revert(None, None, "192.168.1.1", "255.255.255.0") == []
    assert plan_close_ethernet_revert("Ethernet", None, "192.168.1.1", "255.255.255.0") == []


def test_close_revert_bad_subnet_is_safe() -> None:
    assert plan_close_ethernet_revert(
        "Ethernet", "192.168.1.2", "not-an-ip", "255.255.255.0"
    ) == []


# ----- ethernet_revert_pending marker round-trip (Stage-4 polish) -----


def test_settings_ethernet_revert_pending_roundtrip(tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QSettings

    from pingpair import settings

    ini = tmp_path / "rp.ini"
    monkeypatch.setattr(
        settings, "_q", lambda: QSettings(str(ini), QSettings.Format.IniFormat)
    )
    assert settings.load_ethernet_revert_pending() is None
    settings.save_ethernet_revert_pending("Ethernet0")
    assert settings.load_ethernet_revert_pending() == "Ethernet0"
    settings.save_ethernet_revert_pending(None)
    assert settings.load_ethernet_revert_pending() is None
