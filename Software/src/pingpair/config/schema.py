"""Pydantic models for the application config.

Loaded from ``defaults.json`` on startup, optionally overlaid with a
user config file in ``%APPDATA%\\PingPair\\config.json`` (Phase 4+).
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, IPvAnyAddress, field_validator

from ..paths import DEFAULTS_JSON


class NetworkConfig(BaseModel):
    server_ip: IPvAnyAddress
    client_ip: IPvAnyAddress
    subnet_mask: str = "255.255.255.0"

    @field_validator("subnet_mask")
    @classmethod
    def _validate_subnet_mask(cls, v: str) -> str:
        """Reject a malformed netmask at config-load, not at netsh-time.

        A valid IPv4 netmask is a dotted-quad of contiguous 1-bits (e.g.
        ``255.255.255.0``). ``IPv4Network("0.0.0.0/<mask>")`` raises on a
        non-contiguous or non-dotted value, which we surface as a clear
        config error. Defence-in-depth — the netsh path already contains a
        bad mask (list-argv + caught ``ValueError``), this just fails fast.
        """
        try:
            ipaddress.IPv4Network(f"0.0.0.0/{v}", strict=False)
        except (ipaddress.NetmaskValueError, ipaddress.AddressValueError, ValueError) as exc:
            raise ValueError(f"invalid IPv4 subnet mask: {v!r}") from exc
        return v
    # Optional profile-level default gateway. None = point-to-point LAN
    # (current canonical behaviour - no gateway). When set, the Setup tab
    # netsh fix will append `gateway=<gw> gwmetric=1` unless the user has
    # supplied a per-PC override via the Setup tab. Added 2026-05-16 as
    # part of the Group F Setup tab redesign (Q1) + Gateway support.
    gateway: IPvAnyAddress | None = None
    control_port: int = Field(default=5202, ge=1, le=65535)
    iperf3_port: int = Field(default=5201, ge=1, le=65535)


class TestPlanConfig(BaseModel):
    payloads_bytes: list[int] = Field(default_factory=lambda: [200, 600, 1000, 1300])
    bandwidths_mbps: list[int] = Field(default_factory=lambda: [10, 30, 50, 70, 90])
    duration_s: int = Field(default=30, ge=1)
    interval_s: int = Field(default=1, ge=1)
    protocol: Literal["udp", "tcp"] = "udp"


class FpingConfig(BaseModel):
    interval_ms: int = Field(default=10, ge=1)
    extra_args: list[str] = Field(default_factory=lambda: ["-l", "-s", "-D"])


class ReportConfig(BaseModel):
    default_dir: Path | None = None  # None = Software\Reports\
    filename_pattern: str = "PingPair_{date}_{time}"
    formats: list[Literal["docx", "xlsx", "pdf", "txt"]] = Field(
        default_factory=lambda: ["docx", "xlsx"]
    )
    open_after_save: bool = True


class UIConfig(BaseModel):
    theme: Literal["dark", "light"] = "dark"
    live_chart: bool = True


class AppConfig(BaseModel):
    network: NetworkConfig
    test_plan: TestPlanConfig = Field(default_factory=TestPlanConfig)
    fping: FpingConfig = Field(default_factory=FpingConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


def load_default_config() -> AppConfig:
    """Load ``defaults.json`` from disk into a typed :class:`AppConfig`.

    The defaults file is bundled in the package so the app can always
    boot to a known-good state even when QSettings or user profiles
    are missing/corrupted. Pydantic validates the JSON shape on load.
    """
    with open(DEFAULTS_JSON, encoding="utf-8") as f:
        data = json.load(f)
    return AppConfig.model_validate(data)
