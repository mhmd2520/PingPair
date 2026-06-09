"""Import / export of user-editable ``.json`` test-plan profiles.

Backs the Config tab's **Download Template**, **Import config**, and
**Save As** buttons.  Conceptually the file is just a ``defaults.json``
the user can keep in a personal library and load on demand.

Schema rules
------------

* The file format is the same JSON shape as
  :data:`pingpair.paths.DEFAULTS_JSON` — every key under ``network``,
  ``test_plan``, ``fping``, ``report``, ``ui`` is the corresponding
  field on :class:`pingpair.config.schema.AppConfig`.
* Any key starting with ``"_comment"`` (case-sensitive) is treated as a
  free-text comment and stripped before validation.  This is how the
  template carries inline explanations without breaking pydantic.
* Loaders accept partial files — any top-level section that's missing
  is filled in from :func:`load_default_config` first.  Means a user
  can write a 3-line profile that only overrides the test plan.

Errors
------

All public functions raise :class:`ConfigIOError` on any failure (file
not found, bad JSON, schema violation).  The Config tab catches it and
renders the message in its inline status banner — never let a
``pydantic.ValidationError`` traceback reach the GUI.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..paths import CONFIGS_DIR
from .schema import AppConfig, load_default_config

# The standard filename for the downloadable template.  Picked to match
# the wording in the Config tab's button label so users see the same
# name on disk that they clicked to create it.
# Filename for the downloadable template.  Plain ``.json`` — config
# profile files use the single ``.json`` extension throughout.
TEMPLATE_FILENAME = "Template.json"


class ConfigIOError(Exception):
    """Raised for any user-facing failure of the import / export flow.

    Carries a single message string that's safe to render directly in
    the GUI — never embeds a pydantic traceback verbatim.  Look at
    :func:`_format_validation_error` for how schema problems are
    summarised into one or two readable lines.
    """


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


def _template_payload() -> dict[str, Any]:
    """Build the JSON dict written by :func:`write_template`.

    Starts from the validated defaults so the shape can never drift
    from the live :class:`AppConfig` — even if a future commit adds a
    new field, the template auto-includes it.  Comments are layered on
    top as ``_comment_<name>`` siblings (case-insensitive, but
    consistent here) so a power user can read the file like a man
    page.
    """
    cfg = load_default_config()
    data = cfg.model_dump(mode="json")

    # Top-level orientation block.
    head = {
        "_comment_IMPORTANT": (
            "*** RENAME THIS FILE BEFORE EDITING. ***  Clicking 'Download "
            "Template' again will prompt before overwriting this file, but "
            "the safer habit is to rename your edits up front (e.g. "
            "'CarPair-M2M4.json' or 'AcceptanceTest.json') so a careless "
            "click on 'Overwrite template' in the prompt can't lose your "
            "work.  Renamed profiles also show up in the Recent list and "
            "the Import dialog so you can switch between them easily."
        ),
        "_comment": (
            "PingPair configuration profile.  Edit the values below and "
            "load the file via the Config tab's 'Import config' button "
            "(or drag it into the app).  Any '_comment*' key is ignored "
            "by the loader."
        ),
        "_comment_usage": (
            "Only the contents matter to PingPair — the filename is "
            "purely cosmetic in the Recent / Import dialogs.  Pick "
            "whatever describes the profile (customer, hardware, lane)."
        ),
        "_comment_schema": (
            "Shape mirrors src/pingpair/config/defaults.json — every "
            "section is optional; missing sections inherit from the "
            "shipped defaults."
        ),
    }

    # Per-section comments.  Each block is the values from
    # ``cfg.model_dump`` plus a leading ``_comment`` describing the
    # block.  Order matches the AppConfig field order so the file
    # reads top-to-bottom in the same shape as defaults.json.
    network = {
        "_comment": (
            "Network parameters.  ``server_ip`` is the listener side "
            "(Laptop A); ``client_ip`` is the side that drives the "
            "sweep (Laptop B).  ``control_port`` is the TCP channel "
            "PingPair uses to coordinate cases; ``iperf3_port`` is "
            "iperf3's own data port."
        ),
        **data["network"],
    }
    test_plan = {
        "_comment": (
            "The 20-case test grid is the cartesian product of "
            "``payloads_bytes`` x ``bandwidths_mbps``.  Each case runs "
            "for ``duration_s`` seconds.  Use ``protocol = 'tcp'`` to "
            "drop the -u flag (no jitter / loss metrics in that mode)."
        ),
        **data["test_plan"],
    }
    fping = {
        "_comment": (
            "fping's inter-packet interval (ms) and extra CLI flags.  "
            "Default ``-l -s -D`` = loop forever, print summary, "
            "timestamps each line.  PingPair replaces ``-l`` with "
            "``-c <count>`` at runtime so the process self-terminates."
        ),
        **data["fping"],
    }
    report = {
        "_comment": (
            "Default destination, filename pattern, and output formats "
            "for the reports written after every sweep.  These are "
            "*defaults* — the Save Options tab can override them per-run."
        ),
        **data["report"],
    }
    ui = {
        "_comment": "UI defaults.  ``theme`` is reserved for Phase-5 polish.",
        **data["ui"],
    }

    return {
        **head,
        "network": network,
        "test_plan": test_plan,
        "fping": fping,
        "report": report,
        "ui": ui,
    }


def write_template(dest: Path | None = None) -> Path:
    """Write a freshly-commented template file and return its path.

    ``dest`` defaults to ``Software\\Configs\\Template.json``.
    The parent folder is created if necessary; an existing file is
    overwritten (the user explicitly clicked Download Template so a
    re-download is the expected outcome).
    """
    if dest is None:
        dest = CONFIGS_DIR / TEMPLATE_FILENAME
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as f:
            json.dump(_template_payload(), f, indent=2)
            f.write("\n")
    except OSError as exc:
        raise ConfigIOError(
            f"Could not write template to {dest}: {exc}"
        ) from exc
    return dest


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _strip_comments(obj: Any) -> Any:
    """Recursively remove any dict key starting with ``_comment``.

    Lists are walked element-by-element (a list of dicts can carry
    comments too — we don't use that pattern, but it's the safest
    default).  Scalars pass through unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_comments(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.startswith("_comment"))
        }
    if isinstance(obj, list):
        return [_strip_comments(item) for item in obj]
    return obj


def _format_validation_error(exc: ValidationError) -> str:
    """Turn a pydantic ValidationError into a single user-readable line.

    pydantic's default ``str()`` produces a multi-line block with
    "for further information visit ..." URLs.  In the Config tab's
    inline banner that overflows badly; we strip it down to
    ``field.path: message`` per error, semicolon-separated, capped at
    three so the banner stays scannable.
    """
    parts: list[str] = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    overflow = len(exc.errors()) - 3
    if overflow > 0:
        parts.append(f"(+{overflow} more)")
    return "; ".join(parts) or "schema validation failed"


def load_config_file(path: Path) -> AppConfig:
    """Read ``path``, strip comments, merge with defaults, validate.

    Missing top-level sections fall back to the defaults — this lets a
    user write a 5-line file that only overrides the test plan and
    leaves network / fping / report / ui untouched.  Anything else
    raises :class:`ConfigIOError` with a one-line summary.
    """
    if not path.exists():
        raise ConfigIOError(f"File not found: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigIOError(
            f"Could not parse {path.name}: {exc.msg} (line {exc.lineno}, "
            f"column {exc.colno})"
        ) from exc
    except OSError as exc:
        raise ConfigIOError(f"Could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigIOError(
            f"{path.name}: top-level JSON must be an object, got "
            f"{type(raw).__name__}"
        )

    cleaned = _strip_comments(raw)

    # Merge with shipped defaults so partial files are valid.
    defaults = load_default_config().model_dump(mode="json")
    merged = _merge_sections(defaults, cleaned)

    try:
        return AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigIOError(
            f"{path.name} is not a valid PingPair config: "
            f"{_format_validation_error(exc)}"
        ) from exc


def _merge_sections(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Two-level merge: a present section is field-merged onto defaults.

    For each top-level section (network / test_plan / ...) the override's
    fields are layered on top of the default section, so a *partial*
    section works — ``test_plan: {duration_s: 60}`` keeps the default
    payloads, bandwidths, etc., and a config with only ``server_ip`` keeps
    the default ``client_ip``. A section absent from ``overrides`` is
    inherited whole from ``defaults``. Non-dict top-level values are
    replaced outright.

    Unknown keys at the top level pass through so pydantic surfaces
    the typo as a validation error.
    """
    result = deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            # One level of merge for the section itself — within a
            # section, override individual fields but keep unspecified
            # ones from defaults.  This is what makes partial sections
            # work ("test_plan: {duration_s: 60}" keeps payloads etc.).
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def dump_config_file(cfg: AppConfig, dest: Path) -> Path:
    """Write ``cfg`` to ``dest`` as pretty JSON, no comments.

    Used by the Save As button.  The output is round-trip-loadable via
    :func:`load_config_file`.  Parent folder is created.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as f:
            json.dump(cfg.model_dump(mode="json"), f, indent=2)
            f.write("\n")
    except OSError as exc:
        raise ConfigIOError(f"Could not write {dest}: {exc}") from exc
    return dest


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_config(ctx: Any, cfg: AppConfig) -> None:
    """Mutate ``ctx.config`` in place and refresh dependent state.

    AppConfig is a pydantic BaseModel without ``frozen=True`` so its
    sub-models accept attribute assignment.  We replace each section
    field-by-field rather than swapping the whole object so any other
    module that captured a reference to ``ctx.config`` keeps pointing
    at the right instance.

    Also nudges :class:`RunState` to pick up the new defaults — the
    payload / bandwidth dropdowns in the single-case preview should
    track the new test plan's first values — and finally fires every
    listener registered via
    :meth:`AppContext.notify_config_changed_listeners` so the Run
    and Setup tabs can rebuild themselves.

    The Config tab is responsible for guarding against a mid-sweep
    apply — by the time this function is called, the sweep_active
    flag should already be False.
    """
    # In-place replacement of each sub-model preserves identity.
    ctx.config.network = cfg.network
    ctx.config.test_plan = cfg.test_plan
    ctx.config.fping = cfg.fping
    ctx.config.report = cfg.report
    ctx.config.ui = cfg.ui

    # Update RunState single-case defaults to match the new plan's
    # first row.  Don't clobber the user's selection if they were
    # already inside the new payload / bandwidth lists.
    rs = ctx.run_state
    plan = cfg.test_plan
    if plan.payloads_bytes and rs.payload_bytes not in plan.payloads_bytes:
        rs.payload_bytes = plan.payloads_bytes[0]
    if plan.bandwidths_mbps and rs.bandwidth_mbps not in plan.bandwidths_mbps:
        rs.bandwidth_mbps = plan.bandwidths_mbps[0]
    rs.duration_s = plan.duration_s
    rs.protocol = plan.protocol

    # Clear any stale subset that points at indexes outside the new
    # plan length (e.g. previous plan had 20 cases, new one has 12).
    max_idx = len(plan.payloads_bytes) * len(plan.bandwidths_mbps)
    rs.selected_case_indexes = [i for i in rs.selected_case_indexes if 1 <= i <= max_idx]

    # Notify listeners — Run tab rebuilds its sweep grid, Setup
    # tab re-evaluates prereqs against the new IPs, etc.  Wrapped in
    # try/except inside the helper so one bad listener doesn't break
    # the rest.
    notify = getattr(ctx, "notify_config_changed", None)
    if callable(notify):
        notify()


# ---------------------------------------------------------------------------
# Discovery (for the Recent / Browse picker in the Config tab)
# ---------------------------------------------------------------------------


def list_known_configs(root: Path | None = None) -> list[Path]:
    """Return all ``*.json`` profile files in ``root`` (default CONFIGS_DIR).

    Sorted by modification time, newest first — matches the Recent
    reports list pattern on the Save Options tab.  Returns an empty list
    when the folder doesn't exist yet (first-time launch).

    Profile files use the plain ``.json`` suffix.
    """
    folder = root or CONFIGS_DIR
    if not folder.exists():
        return []
    try:
        files = [
            p for p in folder.iterdir()
            if p.is_file() and p.name.lower().endswith(".json")
        ]
    except OSError:
        return []
    # Stat each file ONCE inside a guard. A bare ``key=lambda p: p.stat()``
    # re-stats during the sort and raises OSError if a file is deleted
    # between iterdir() and the sort, or on a flaky network share — which
    # would blow up the whole listing. Mirror enumerate_sidecars: drop
    # un-stattable files (mtime 0.0 sorts them last) instead.
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=_mtime, reverse=True)
    return files


__all__ = [
    "ConfigIOError",
    "TEMPLATE_FILENAME",
    "apply_config",
    "dump_config_file",
    "list_known_configs",
    "load_config_file",
    "write_template",
]
