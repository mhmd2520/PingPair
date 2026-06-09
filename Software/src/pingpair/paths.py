"""Filesystem path helpers.

Single source of truth for where things live on disk.  All other modules
should import from here rather than hardcoding paths, so packaging
(PyInstaller, Phase 5) only has one place to adjust.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _project_root() -> Path:
    """Return the root that bundled ``bin/`` + data sit under, dev or frozen.

    - Dev:    src/pingpair/paths.py  →  parents[2] is Software/, with bin/ below.
    - Frozen: ``sys._MEIPASS`` is the bundle's data root — the one-folder
      ``_internal/`` dir (PyInstaller 6.x) or the one-file temp-extract dir.
      The spec copies ``bin/fping`` + ``bin/iperf3`` under it, so resolving
      from ``_MEIPASS`` keeps ``BIN_DIR`` valid in both layouts; we fall back
      to the executable's own dir if ``_MEIPASS`` is somehow unset.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT: Path = _project_root()
"""The Software/ folder."""

BIN_DIR: Path = PROJECT_ROOT / "bin"
FPING_DIR: Path = BIN_DIR / "fping"
IPERF3_DIR: Path = BIN_DIR / "iperf3"

FPING_EXE: Path = FPING_DIR / "fping.exe"
IPERF3_EXE: Path = IPERF3_DIR / "iperf3.exe"

def _reports_root() -> Path:
    """Where run reports are written.

    * **Dev:** ``Software/Reports`` (next to the source tree).
    * **Frozen:** **beside the ``.exe``** (``<install>/Reports``), *not* inside
      ``_internal/``. Two reasons: (1) users find their reports right next to
      ``PingPair.exe`` instead of digging into the runtime-files folder, and
      (2) the self-update mirrors ``_internal`` with ``robocopy /MIR``, which
      would *delete* a ``_internal/Reports`` on every update — beside the exe
      it's out of that path, and the swap helper additionally ``/XD``-excludes
      it (see :func:`pingpair.core.update_apply.build_swap_script`).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "Reports"
    return PROJECT_ROOT / "Reports"


REPORTS_DIR: Path = _reports_root()
CONFIGS_DIR: Path = PROJECT_ROOT / "Configs"
"""User-facing folder for importable / exportable ``.json`` test-plan
profiles. Symmetric with :data:`REPORTS_DIR` so the Config tab's Download
Template / Save As targets are easy to find next to the run reports.

Auto-created on first use by :mod:`config.config_io` (no need to ship an
empty folder in the source tree)."""

RESOURCES_DIR: Path = Path(__file__).resolve().parent / "resources"
HELP_DIR: Path = RESOURCES_DIR / "help"
"""Folder-based Help guide root: one ``<NN-slug>/index.html`` per section,
walked by :mod:`pingpair.help_loader`. Lives under :data:`RESOURCES_DIR` so it
resolves identically in dev and in a frozen one-folder build; the HTML + image
assets only ship in a wheel / frozen build if ``pyproject.toml``'s
``package-data`` lists the help tree."""

CONFIG_DIR: Path = Path(__file__).resolve().parent / "config"
DEFAULTS_JSON: Path = CONFIG_DIR / "defaults.json"


def user_data_dir() -> Path:
    """Per-user PingPair directory for logs, settings, etc.

    Resolves to ``%APPDATA%\\PingPair`` on Windows,
    ``~/.local/share/PingPair`` on Linux,
    ``~/Library/Application Support/PingPair`` on macOS.
    """
    import os
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "PingPair"


def log_dir() -> Path:
    return user_data_dir() / "logs"
