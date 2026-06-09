"""Tests for the ``config_io`` import / export / apply helpers.

Covers the on-disk shape (template + save round-trip), partial-config
merging with defaults, comment stripping, friendly error messages on
malformed input, and the AppContext apply / notify path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pingpair.config import (
    AppConfig,
    ConfigIOError,
    apply_config,
    dump_config_file,
    list_known_configs,
    load_config_file,
    load_default_config,
    write_template,
)
from pingpair.config.config_io import _merge_sections, _strip_comments


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


def test_write_template_creates_loadable_file(tmp_path: Path) -> None:
    dest = tmp_path / "Template.json"
    written = write_template(dest)
    assert written == dest
    assert dest.exists()

    # The file we just wrote should round-trip through the loader.
    cfg = load_config_file(dest)
    assert isinstance(cfg, AppConfig)
    # Default plan is the 4x5 grid.
    assert len(cfg.test_plan.payloads_bytes) == 4
    assert len(cfg.test_plan.bandwidths_mbps) == 5


def test_template_has_top_level_and_section_comments(tmp_path: Path) -> None:
    """The template should ship with at least one `_comment*` per section.

    Comments are stripped on load (asserted elsewhere) but should be
    present on disk so a human editor sees inline guidance.
    """
    dest = tmp_path / "Template.json"
    write_template(dest)
    raw = json.loads(dest.read_text(encoding="utf-8"))
    assert any(k.startswith("_comment") for k in raw), \
        "top level should have at least one _comment key"
    for section in ("network", "test_plan", "fping", "report", "ui"):
        assert any(
            k.startswith("_comment") for k in raw[section]
        ), f"section '{section}' should have at least one _comment key"


def test_write_template_creates_parent_dir(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "subdir" / "Template.json"
    write_template(dest)
    assert dest.exists()


# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------


def test_strip_comments_removes_only_underscore_comment_keys() -> None:
    raw = {
        "_comment": "header",
        "_comment_extra": "more",
        "network": {
            "_comment": "section header",
            "server_ip": "192.168.1.1",
        },
        "test_plan": {
            "payloads_bytes": [200, 600],
            "_comment": "x",
        },
    }
    cleaned = _strip_comments(raw)
    assert "_comment" not in cleaned
    assert "_comment_extra" not in cleaned
    assert "_comment" not in cleaned["network"]
    assert "_comment" not in cleaned["test_plan"]
    # Non-comment data preserved.
    assert cleaned["network"]["server_ip"] == "192.168.1.1"
    assert cleaned["test_plan"]["payloads_bytes"] == [200, 600]


def test_strip_comments_handles_nested_lists() -> None:
    raw = [{"_comment": "x", "value": 1}, {"value": 2}]
    cleaned = _strip_comments(raw)
    assert cleaned == [{"value": 1}, {"value": 2}]


def test_strip_comments_preserves_keys_that_only_share_a_prefix() -> None:
    # "comment" (no underscore) is NOT a magic key — should survive.
    raw = {"comment": "kept", "_comment": "stripped"}
    cleaned = _strip_comments(raw)
    assert cleaned == {"comment": "kept"}


# ---------------------------------------------------------------------------
# Section merging
# ---------------------------------------------------------------------------


def test_merge_sections_partial_test_plan_keeps_defaults() -> None:
    """A 1-field test_plan override should preserve the other plan fields."""
    defaults = load_default_config().model_dump(mode="json")
    overrides = {"test_plan": {"duration_s": 60}}
    merged = _merge_sections(defaults, overrides)
    # Duration overridden, payloads inherited.
    assert merged["test_plan"]["duration_s"] == 60
    assert merged["test_plan"]["payloads_bytes"] == [200, 600, 1000, 1300]


def test_merge_sections_replaces_scalar_fields() -> None:
    defaults = {"foo": "bar", "n": 1}
    merged = _merge_sections(defaults, {"n": 99, "new": "x"})
    assert merged == {"foo": "bar", "n": 99, "new": "x"}


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def test_load_partial_config_inherits_defaults(tmp_path: Path) -> None:
    """A 5-line file with only test_plan should still produce a valid config."""
    src = tmp_path / "partial.config.json"
    src.write_text(
        json.dumps({"test_plan": {"duration_s": 45, "payloads_bytes": [500]}}),
        encoding="utf-8",
    )
    cfg = load_config_file(src)
    assert cfg.test_plan.duration_s == 45
    assert cfg.test_plan.payloads_bytes == [500]
    # Network came from defaults.
    assert str(cfg.network.server_ip) == "192.168.1.1"


def test_load_missing_file_raises_io_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigIOError, match="not found"):
        load_config_file(tmp_path / "nope.config.json")


def test_load_malformed_json_raises_io_error(tmp_path: Path) -> None:
    src = tmp_path / "broken.config.json"
    src.write_text("{this is not json", encoding="utf-8")
    with pytest.raises(ConfigIOError) as exc_info:
        load_config_file(src)
    # Friendly error includes line/column hint.
    assert "line" in str(exc_info.value).lower()


def test_load_non_object_root_raises_io_error(tmp_path: Path) -> None:
    src = tmp_path / "list_root.config.json"
    src.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigIOError, match="object"):
        load_config_file(src)


def test_load_schema_violation_raises_io_error(tmp_path: Path) -> None:
    """Invalid IP should surface a one-line summary, not a pydantic traceback."""
    src = tmp_path / "bad_ip.config.json"
    src.write_text(
        json.dumps({"network": {"server_ip": "not-an-ip"}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigIOError) as exc_info:
        load_config_file(src)
    msg = str(exc_info.value)
    assert "not a valid" in msg or "network" in msg or "server_ip" in msg


# ---------------------------------------------------------------------------
# Save round-trip
# ---------------------------------------------------------------------------


def test_save_load_round_trip(tmp_path: Path) -> None:
    cfg = load_default_config()
    # Mutate something so the round-trip isn't a no-op.
    cfg.test_plan.duration_s = 99
    cfg.test_plan.payloads_bytes = [123]
    dest = tmp_path / "round.config.json"
    dump_config_file(cfg, dest)
    reloaded = load_config_file(dest)
    assert reloaded.test_plan.duration_s == 99
    assert reloaded.test_plan.payloads_bytes == [123]
    # Other fields untouched.
    assert reloaded.test_plan.bandwidths_mbps == cfg.test_plan.bandwidths_mbps


def test_dump_creates_parent_dir(tmp_path: Path) -> None:
    cfg = load_default_config()
    dest = tmp_path / "a" / "b" / "c.config.json"
    dump_config_file(cfg, dest)
    assert dest.exists()


# ---------------------------------------------------------------------------
# Apply / notify
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> Any:
    """Build a SimpleNamespace shaped like AppContext for apply_config().

    Avoids importing AppContext from context.py because that drags in
    QSettings and would slow these unit tests.
    """
    cfg = load_default_config()
    rs = SimpleNamespace(
        payload_bytes=200,
        bandwidth_mbps=10,
        duration_s=30,
        protocol="udp",
        selected_case_indexes=[1, 5, 11],
    )

    listeners: list[Any] = []

    fired: list[str] = []

    def notify() -> None:
        fired.append("ok")
        for cb in listeners:
            cb()

    ctx = SimpleNamespace(
        config=cfg,
        run_state=rs,
        config_changed_listeners=listeners,
        notify_config_changed=notify,
        logger=logging.getLogger("test_config_io"),
        _fired=fired,
    )
    return ctx


def test_apply_config_mutates_in_place_and_notifies() -> None:
    ctx = _make_fake_ctx()
    new_cfg = load_default_config()
    new_cfg.test_plan.payloads_bytes = [100, 200]
    new_cfg.test_plan.bandwidths_mbps = [10, 20, 30]
    new_cfg.test_plan.duration_s = 5
    new_cfg.test_plan.protocol = "tcp"

    apply_config(ctx, new_cfg)

    # Sub-models mutated in place — same parent object identity.
    assert ctx.config.test_plan.payloads_bytes == [100, 200]
    assert ctx.config.test_plan.bandwidths_mbps == [10, 20, 30]
    assert ctx.config.test_plan.duration_s == 5
    assert ctx.config.test_plan.protocol == "tcp"
    # RunState picked up new defaults.
    assert ctx.run_state.duration_s == 5
    assert ctx.run_state.protocol == "tcp"
    # Notify was called.
    assert ctx._fired == ["ok"]


def test_apply_config_drops_subset_indexes_outside_new_plan() -> None:
    """selected_case_indexes [1, 5, 11] vs 2×3=6-case plan -> keep [1, 5]."""
    ctx = _make_fake_ctx()
    ctx.run_state.selected_case_indexes = [1, 5, 11]
    new_cfg = load_default_config()
    new_cfg.test_plan.payloads_bytes = [100, 200]
    new_cfg.test_plan.bandwidths_mbps = [10, 20, 30]
    apply_config(ctx, new_cfg)
    assert ctx.run_state.selected_case_indexes == [1, 5]


def test_apply_config_keeps_runstate_payload_if_still_valid() -> None:
    """If user's current payload still appears in the new plan, don't reset it."""
    ctx = _make_fake_ctx()
    ctx.run_state.payload_bytes = 600  # default plan's second payload
    new_cfg = load_default_config()
    # Keep 600 in payloads — should be preserved.
    new_cfg.test_plan.payloads_bytes = [200, 600, 999]
    apply_config(ctx, new_cfg)
    assert ctx.run_state.payload_bytes == 600


def test_apply_config_resets_runstate_payload_if_not_in_new_plan() -> None:
    ctx = _make_fake_ctx()
    ctx.run_state.payload_bytes = 600
    new_cfg = load_default_config()
    new_cfg.test_plan.payloads_bytes = [100, 200, 300]  # 600 missing
    apply_config(ctx, new_cfg)
    assert ctx.run_state.payload_bytes == 100


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_list_known_configs_finds_config_json_files(tmp_path: Path) -> None:
    """Recognises .config.json, .config, AND .json.

    .config files turn up when Notepad++ on Windows strips the trailing
    .json from a user-typed .config suffix; we treat them as first-
    class PingPair config files because the on-disk content is still
    valid JSON.
    """
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("x", encoding="utf-8")
    (tmp_path / "no_extension_at_all").write_text("{}", encoding="utf-8")
    (tmp_path / "old_format.config").write_text("{}", encoding="utf-8")
    # Old-style sidecar names end with `.json`, so the filter
    # naturally accepts them — that's free back-compat for any
    # legacy file the user might still have lying around.
    (tmp_path / "old_format.config.json").write_text("{}", encoding="utf-8")

    found = list_known_configs(tmp_path)
    names = sorted(p.name for p in found)
    # Recent-profiles list contains every file ending in `.json`.
    # `.config` files don't match (no `.json` suffix); `.txt` and
    # extensionless files don't match either.
    assert names == ["a.json", "b.json", "old_format.config.json"]


def test_load_handles_plain_json_extension(tmp_path: Path) -> None:
    """Plain .json files (the canonical extension) load cleanly."""
    src = tmp_path / "MyProfile.json"
    src.write_text(
        json.dumps({"test_plan": {"duration_s": 7}}),
        encoding="utf-8",
    )
    cfg = load_config_file(src)
    assert cfg.test_plan.duration_s == 7



def test_template_filename_is_plain_json() -> None:
    """The default template filename should be 'Template.json'.

    Switched from the legacy 'Template.config.json' on 2026-05-15
    because Qt's QFileDialog auto-append on Windows produced
    'Test.config.config.json' artefacts when users typed a '.config'
    suffix.  Pin the new name so it can't regress.
    """
    from pingpair.config import TEMPLATE_FILENAME
    assert TEMPLATE_FILENAME == "Template.json"


def test_list_known_configs_returns_empty_when_folder_missing(tmp_path: Path) -> None:
    """No exception when Configs/ doesn't exist (first-time launch)."""
    missing = tmp_path / "nonexistent"
    assert list_known_configs(missing) == []


def test_template_with_comments_loads_cleanly(tmp_path: Path) -> None:
    """End-to-end: write_template -> load_config_file round-trip.

    The on-disk template is full of `_comment*` keys; the loader
    strips them before pydantic validation.  This pins the contract
    that the freshly-written template is always immediately
    loadable.
    """
    dest = tmp_path / "Template.json"
    write_template(dest)
    cfg = load_config_file(dest)
    defaults = load_default_config()
    assert cfg.test_plan.payloads_bytes == defaults.test_plan.payloads_bytes
    assert cfg.test_plan.bandwidths_mbps == defaults.test_plan.bandwidths_mbps
    assert cfg.test_plan.duration_s == defaults.test_plan.duration_s


# ---------------------------------------------------------------------------
# NetworkConfig.subnet_mask validation (Stage-4 polish, 2026-06-03)
# ---------------------------------------------------------------------------


def test_network_config_accepts_valid_subnet_mask() -> None:
    from pingpair.config.schema import NetworkConfig

    cfg = NetworkConfig(server_ip="192.168.1.1", client_ip="192.168.1.2",
                        subnet_mask="255.255.255.0")
    assert cfg.subnet_mask == "255.255.255.0"
    # A /16 mask is also valid.
    cfg2 = NetworkConfig(server_ip="10.0.0.1", client_ip="10.0.0.2",
                         subnet_mask="255.255.0.0")
    assert cfg2.subnet_mask == "255.255.0.0"


def test_network_config_rejects_malformed_subnet_mask() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    from pingpair.config.schema import NetworkConfig

    # Non-contiguous mask.
    with _pytest.raises(ValidationError):
        NetworkConfig(server_ip="192.168.1.1", client_ip="192.168.1.2",
                      subnet_mask="255.255.0.255")
    # Not a dotted-quad at all.
    with _pytest.raises(ValidationError):
        NetworkConfig(server_ip="192.168.1.1", client_ip="192.168.1.2",
                      subnet_mask="not-a-mask")
