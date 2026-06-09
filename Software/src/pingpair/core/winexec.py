"""Resolve Windows system tools to their absolute ``System32`` path.

PingPair runs **elevated** (it shells out to ``netsh`` to change IPs /
firewall rules / Wi-Fi state, ``ping`` for the gateway probe, ``icacls`` to
lock the update staging dir, and ``cmd`` to launch the swap helper).
Invoking those by bare name resolves them through ``%PATH%`` and — for some
APIs — the current working directory, so a planted ``netsh.exe`` /
``icacls.exe`` earlier on ``PATH`` or in the cwd would execute with
Administrator rights. That is the classic *untrusted search path*
local-privilege-escalation surface (CWE-426).

Pinning each tool to ``%SystemRoot%\\System32\\<tool>.exe`` removes the
search entirely. This is a no-op off Windows and falls back to the bare
name when the absolute path can't be resolved (so a misconfigured host
still works exactly as before — we only ever *tighten*, never break).
"""

from __future__ import annotations

import os
import sys

# Bare names PingPair invokes that live in System32. Kept as a small
# allow-list so we never rewrite the path of a bundled tool (iperf3 / fping
# already resolve to absolute bundled paths via ``paths.py``).
_SYSTEM_TOOLS = frozenset(
    {"netsh", "ping", "icacls", "cmd", "robocopy", "tasklist", "find"}
)


def system_tool(name: str) -> str:
    """Return the absolute ``System32`` path for a known system tool.

    Returns ``name`` unchanged off Windows, for an unknown tool, or when the
    expected executable isn't present at the resolved location.
    """
    base = name[:-4] if name.lower().endswith(".exe") else name
    if sys.platform != "win32" or base.lower() not in _SYSTEM_TOOLS:
        return name
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = os.path.join(root, "System32", base + ".exe")
    return candidate if os.path.isfile(candidate) else name


def harden_argv(argv: list[str]) -> list[str]:
    """Return ``argv`` with element 0 resolved to its absolute System32 path.

    Builders elsewhere produce ``["netsh", ...]`` for readability and so
    their unit tests can assert on the plain name; this is applied at the
    *execution* site only, right before the process is spawned.
    """
    if not argv:
        return argv
    return [system_tool(argv[0]), *argv[1:]]
