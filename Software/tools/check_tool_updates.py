"""Dev-side watcher for upstream fping / iperf3 releases (Feature #14).

NOT shipped in the app — a maintainer convenience run from the repo. It:

1. probes the **bundled** binaries (``Software/bin/fping``, ``Software/bin/iperf3``)
   for their current version (no hard-coded constant — always accurate);
2. queries each tool's upstream GitHub "latest release";
3. reports whether an upgrade is available, with the link and the
   integration path for each tool.

Integration differs per tool (this is why a human runs the upgrade):

* **fping** ships **source only** — a new version must be **rebuilt from source
  under Cygwin** via ``Build/fping_5.5/build-fping-5.5.sh`` (adapt the version),
  then the rebuilt ``fping.exe`` + its ``cygwin1.dll`` copied into
  ``Software/bin/fping``.
* **iperf3** ships a **prebuilt** Windows/Cygwin build — swap the new
  ``iperf3.exe`` + its Cygwin runtime DLLs into ``Software/bin/iperf3``.

Reuses :func:`pingpair.core.updater.fetch_latest_release` (generic, takes a URL)
and :func:`pingpair.core.updater.is_newer` so the version logic stays in one
place. See the ``check-tool-updates`` skill for the full upgrade checklist.

Usage::

    Software\\.venv\\Scripts\\python.exe Software\\tools\\check_tool_updates.py

Exit code 0 = checked OK (whether or not an upgrade exists), 1 = a check failed
(e.g. network/API error), so CI / a cron can alert on a hard failure.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Run straight from the repo without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pingpair import paths  # noqa: E402
from pingpair.core import updater  # noqa: E402

_FPING_RE = re.compile(r"[Vv]ersion\s+(\d+(?:\.\d+)+)")
_IPERF3_RE = re.compile(r"\biperf\s+(\d+(?:\.\d+)+)", re.IGNORECASE)

TOOLS = (
    {
        "name": "fping",
        "exe": paths.FPING_EXE,
        "version_args": ["-v"],
        "regex": _FPING_RE,
        "repo": "schweikert/fping",
        "releases_url": "https://github.com/schweikert/fping/releases",
        "integration": (
            "REBUILD FROM SOURCE under Cygwin — fping ships source only.\n"
            "       Adapt Build/fping_5.5/build-fping-5.5.sh to the new version,\n"
            "       rebuild, then copy fping.exe + its cygwin1.dll into\n"
            "       Software/bin/fping. Verify every test-plan flag still exists\n"
            "       and the output format is byte-identical before shipping."
        ),
    },
    {
        "name": "iperf3",
        "exe": paths.IPERF3_EXE,
        "version_args": ["--version"],
        "regex": _IPERF3_RE,
        "repo": "esnet/iperf",
        "releases_url": "https://github.com/esnet/iperf/releases",
        "integration": (
            "SWAP THE PREBUILT Windows/Cygwin build into Software/bin/iperf3\n"
            "       (iperf3 ships a prebuilt binary). Mind the bundled Cygwin\n"
            "       runtime DLL versions (cygwin1.dll / cygcrypto-*.dll) — they\n"
            "       differ from fping's copy; keep each tool's own DLLs."
        ),
    },
)


def _bundled_version(exe: Path, args: list[str], regex: re.Pattern[str]) -> str:
    """Run the bundled binary and parse its version, or '' if unavailable.

    Runs with ``cwd`` set to the binary's own folder so Windows resolves that
    tool's Cygwin DLLs first (the project's per-tool convention). Both fping
    and iperf3 print the version to stdout or stderr, so we scan both.
    """
    if not exe.is_file():
        return ""
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [str(exe), *args],
            cwd=str(exe.parent),
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    blob = f"{proc.stdout}\n{proc.stderr}"
    m = regex.search(blob)
    return m.group(1) if m else ""


def _check_one(tool: dict) -> bool:
    """Print the status for one tool. Return False on a hard check failure."""
    name = tool["name"]
    current = _bundled_version(tool["exe"], tool["version_args"], tool["regex"])
    cur_txt = current or "unknown (binary not found / unparseable)"

    api = f"https://api.github.com/repos/{tool['repo']}/releases/latest"
    try:
        data = updater.fetch_latest_release(api)
    except updater.UpdateCheckError as exc:
        print(f"[{name}] bundled {cur_txt} — could not check upstream: {exc}")
        return False

    latest = str(data.get("tag_name", "")).strip().lstrip("vV").strip()
    if not latest:
        print(f"[{name}] bundled {cur_txt} — upstream returned no version tag.")
        return True

    if not current:
        print(
            f"[{name}] bundled {cur_txt}; upstream latest {latest}. "
            f"Probe the bundled binary manually to compare."
        )
        return True

    if updater.is_newer(latest, current):
        print(f"[{name}] UPGRADE AVAILABLE: bundled {current} -> upstream {latest}")
        print(f"       Release: {tool['releases_url']}/tag/{data.get('tag_name', latest)}")
        print(f"       {tool['integration']}")
    else:
        print(f"[{name}] up to date (bundled {current}, upstream {latest}).")
    return True


def main() -> int:
    print("PingPair bundled-tool update check (fping / iperf3)\n")
    ok = True
    for tool in TOOLS:
        ok = _check_one(tool) and ok
        print()
    if not ok:
        print("One or more checks could not reach upstream.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
