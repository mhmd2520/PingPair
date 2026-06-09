"""Entry point: ``python -m pingpair``."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys

from .config import load_default_config
from .context import AppContext
from .core.prereq import Status, has_blockers, run_checks


def _default_is_admin() -> bool | None:
    """``True`` / ``False`` if we can tell, ``None`` if the check itself failed."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return None


def _default_elevate() -> int | None:
    """Relaunch elevated via UAC; return ShellExecute's rc, or ``None`` on failure.

    rc > 32 means the elevated child is launching (UAC accepted); rc <= 32
    means UAC was denied (5 = SE_ERR_ACCESSDENIED) or the call failed.
    """
    try:
        import ctypes
    except ImportError:
        return None
    exe = sys.executable
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        params = subprocess.list2cmdline(["-m", "pingpair", *sys.argv[1:]])
    try:
        return int(
            ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        )
    except (AttributeError, OSError):
        return None


def _ensure_admin_or_relaunch_on_windows(
    *,
    platform: str | None = None,
    no_elevate: bool | None = None,
    is_admin=None,
    elevate=None,
    exit_fn=None,
) -> None:
    """Require Administrator: elevate via UAC, and refuse to run unelevated.

    Almost everything PingPair does to set up a test (firewall rules, static
    IP, toggling Wi-Fi via ``netsh``) needs Administrator, so the app must
    run elevated. On Windows, if we aren't already admin we prompt for UAC
    and relaunch the elevated copy. **If the user denies UAC (or elevation
    fails) we exit — PingPair does not run unelevated.** No message box is
    shown: the UAC prompt the user just dismissed is feedback enough.

    Exceptions, by design:

    * ``PINGPAIR_NO_AUTO_ELEVATE=1`` skips the whole dance (IDE / debugger /
      CI opt-out), so a dev session can run unelevated on purpose.
    * If the admin check itself fails (``IsUserAnAdmin`` raises) we can't
      tell our state, so we run rather than risk a relaunch loop or locking
      the user out of a box where elevation is unavailable.

    The keyword-only parameters exist purely for unit tests; production
    passes none and the module-level defaults take over.
    """
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return
    if no_elevate is None:
        no_elevate = os.environ.get("PINGPAIR_NO_AUTO_ELEVATE") == "1"
    if no_elevate:
        return

    is_admin = is_admin or _default_is_admin
    elevate = elevate or _default_elevate
    exit_fn = exit_fn or sys.exit

    admin = is_admin()
    if admin is None:
        return  # can't determine — don't risk a relaunch loop / lockout
    if admin:
        return

    rc = elevate()
    if rc is not None and int(rc) > 32:
        exit_fn(0)  # UAC accepted — the elevated copy is launching
        return

    # Not admin, and UAC was denied or elevation failed. Admin is mandatory —
    # exit silently (no message box; the UAC prompt was the user's cue).
    exit_fn(1)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pingpair",
        description="Automated LAN characterization using fping and iperf3.",
    )
    parser.add_argument(
        "--check-prereqs",
        action="store_true",
        help="Run the prerequisite checker headless and exit non-zero on any FAIL.",
    )
    parser.add_argument(
        "--loopback",
        action="store_true",
        help="Dev mode: run both Server and Client roles on 127.0.0.1.",
    )
    return parser


def _print_check_results(results: list) -> None:
    """Pretty-print the check table to stdout. Avoids importing tabulate so this
    works even before optional deps are installed."""
    # Windows consoles (and a frozen build's stdout) default to cp1252, which
    # can't encode the box-drawing rule + middot below. Force UTF-8 when stdout
    # is a real text stream; a no-op under pytest capture or a redirected pipe.
    if isinstance(sys.stdout, io.TextIOWrapper):
        with contextlib.suppress(ValueError, OSError):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rows = [(r.status.value.upper(), r.name, r.detail) for r in results]
    name_w = max((len(r[1]) for r in rows), default=10)
    print("─" * 70)
    print(f"{'STATUS':<6}  {'CHECK':<{name_w}}  DETAIL")
    print("─" * 70)
    for status, name, detail in rows:
        print(f"{status:<6}  {name:<{name_w}}  {detail}")
    print("─" * 70)
    counts = {s: sum(1 for r in results if r.status is s) for s in Status}
    print(
        f"{counts[Status.PASS]} pass · {counts[Status.WARN]} warn · "
        f"{counts[Status.FAIL]} fail · {counts[Status.SKIP]} skipped"
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    # Require Administrator for the GUI launch on Windows (elevate via UAC,
    # exit if denied). Headless --check-prereqs is read-only and runs
    # unattended (CI), so it intentionally skips the elevation gate.
    if not args.check_prereqs:
        _ensure_admin_or_relaunch_on_windows()

    ctx = AppContext.create(load_default_config(), loopback=args.loopback)
    # Initial log line just records that the process started; the role
    # comes from QSettings during launch_gui and is logged there with
    # the full state so this top-level line is intentionally minimal.
    ctx.logger.info("PingPair process started (cli loopback=%s)", args.loopback)

    if args.check_prereqs:
        results = run_checks(ctx.config)
        _print_check_results(results)
        return 1 if has_blockers(results) else 0

    # Defer Qt import so --check-prereqs works on a box without PySide6.
    from .app import launch_gui

    return launch_gui(ctx, loopback=args.loopback)


if __name__ == "__main__":
    sys.exit(main())
