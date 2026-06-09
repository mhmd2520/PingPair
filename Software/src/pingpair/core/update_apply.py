"""Apply a downloaded PingPair update (Feature 6).

The hard part of self-update on Windows is that a **running** one-folder
PyInstaller build holds ``PingPair.exe`` and its ``_internal\\*.dll`` open, so
the process cannot overwrite its own install in place. The standard fix: hand
the swap to an **external helper** that waits for this process to exit, mirrors
the freshly-extracted new build over the (now-unlocked) install folder, and
relaunches. We write that helper as a ``.cmd`` (``cmd.exe`` is always present),
launch it detached, and quit.

Layout assumed (PyInstaller one-folder, 6.x):

    <install_dir>\\PingPair.exe
    <install_dir>\\_internal\\...        (Qt, Python, our bundled data)

``<install_dir>`` is ``Path(sys.executable).parent`` in a frozen build.

Modes:

* **frozen** — the real production path: extract the downloaded bundle zip to
  a staging folder, then launch the swap helper + ask the caller to quit.
* **source / dev** (``python -m pingpair``) — there is nothing to swap (the
  code is a git checkout); :func:`apply_update` raises
  :class:`SourceInstallError` so the UI can tell the user to ``git pull``
  instead. The packaged build is what we actually self-update.

Pure, unit-testable helpers (no process/IO side effects) are split out:
:func:`is_frozen`, :func:`install_dir`, :func:`locate_bundle_root`,
:func:`build_swap_script`, and :func:`extract_zip` (IO but deterministic and
guarded against zip-slip).
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

from ..paths import user_data_dir
from .winexec import harden_argv

EXE_NAME = "PingPair.exe"


class UpdateApplyError(Exception):
    """A downloaded update could not be staged or applied."""


class SourceInstallError(UpdateApplyError):
    """Self-update was attempted on a source (non-frozen) checkout.

    Distinct so the UI can show "you're running from source — update via
    git" rather than a generic failure.
    """


def is_frozen() -> bool:
    """True when running as a packaged (PyInstaller) build."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """The folder to overwrite on update — the frozen ``.exe``'s directory."""
    return Path(sys.executable).resolve().parent


def update_cache_dir() -> Path:
    """Per-user scratch dir for downloads + staging + the swap helper."""
    return user_data_dir() / "update"


def extract_zip(zip_path: Path, dest: Path) -> Path:
    """Safely extract ``zip_path`` into ``dest`` (created fresh).

    Guards against zip-slip: any member whose resolved path escapes ``dest``
    aborts the whole extraction. Returns ``dest``.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if dest_resolved != target and dest_resolved not in target.parents:
                raise UpdateApplyError(
                    f"Unsafe path in update archive: {member!r}"
                )
        zf.extractall(dest)
    return dest


def locate_bundle_root(staged: Path, exe_name: str = EXE_NAME) -> Path:
    """Find the folder that contains ``exe_name`` within an extracted bundle.

    The release zip may wrap the build in a top folder (``PingPair/PingPair.exe``)
    or place it at the root (``PingPair.exe``). Checks the root, then any
    immediate sub-directory. Raises :class:`UpdateApplyError` if the exe
    isn't found (a malformed / wrong archive).
    """
    if (staged / exe_name).is_file():
        return staged
    for child in sorted(p for p in staged.iterdir() if p.is_dir()):
        if (child / exe_name).is_file():
            return child
    raise UpdateApplyError(
        f"{exe_name} not found in the downloaded update archive."
    )


def build_swap_script(
    pid: int,
    new_root: Path,
    install_target: Path,
    exe_name: str = EXE_NAME,
    log_path: Path | None = None,
) -> str:
    """Return the ``.cmd`` text that swaps the install and relaunches.

    Steps the script performs:

    1. Poll until process ``pid`` (this app) has exited and released its
       file locks, plus a short grace.
    2. ``robocopy /MIR`` the new build over the install folder (mirror, so
       removed files are cleaned up too). The helper lives in the cache dir,
       not the install folder, so ``/MIR`` never deletes the running script.
    3. Relaunch ``exe_name`` from the install folder.
    4. Remove the staging folder.

    Implementation notes (why this shape):

    * Delays use ``ping -n`` rather than ``timeout``. ``timeout`` reads stdin
      and **fails instantly** ("Input redirection is not supported") when the
      script runs without an interactive console — the exact failure that made
      an earlier version silently never swap. ``ping`` needs no console.
    * The helper runs **silently** (no visible window — see
      :func:`_launch_detached`), so the GUI's "PingPair will now close and
      reopen" dialog is the user's only feedback. Because the window is hidden,
      the script must **never block on input** — so there is NO ``pause``; a
      failed robocopy logs the reason and relaunches the *unchanged* install
      instead of hanging invisibly. Every step is appended to ``log_path`` so a
      failed swap is fully diagnosable after the fact.
    * robocopy's exit code is checked (>= 8 means a real failure). On both the
      success and failure paths the install is relaunched, so the app always
      comes back up.

    Pure string builder — no side effects — so it's unit-testable.
    """
    new_root_s = str(new_root)
    install_s = str(install_target)
    log_s = str(log_path) if log_path is not None else str(new_root.parent / "update_log.txt")
    # Defence-in-depth: these paths are interpolated into a generated .cmd. They
    # are app-derived today (install dir + %APPDATA% cache, not user input), but
    # a path containing a double-quote or CR/LF would break out of the script
    # quoting — refuse rather than emit an injectable helper (CWE-78, latent).
    for label, value in (
        ("new_root", new_root_s),
        ("install_target", install_s),
        ("log_path", log_s),
        ("exe_name", exe_name),
    ):
        if any(ch in value for ch in ('"', "\r", "\n")):
            raise UpdateApplyError(
                f"refusing to build update helper: {label} contains an unsafe "
                f"character ({value!r})."
            )
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        # Pin PATH to System32 so the bare-name tools below (tasklist, ping,
        # robocopy, find) resolve only from the trusted system dir — this .cmd
        # runs ELEVATED with a per-user-writable cwd, so a planted robocopy.exe
        # would otherwise run as admin (CWE-426 untrusted search path).
        'set "PATH=%SystemRoot%\\System32;%SystemRoot%"\r\n'
        f'set "LOG={log_s}"\r\n'
        'echo PingPair update helper started %DATE% %TIME%> "%LOG%"\r\n'
        'echo Waiting for PingPair to close...>> "%LOG%"\r\n'
        ":waitloop\r\n"
        f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "    ping -n 2 127.0.0.1 >nul\r\n"
        "    goto waitloop\r\n"
        ")\r\n"
        'echo Process closed; letting file locks release...>> "%LOG%"\r\n'
        "ping -n 3 127.0.0.1 >nul\r\n"
        'echo Installing update...>> "%LOG%"\r\n'
        # /XD excludes the user's Reports folder (it lives beside the .exe, in
        # the install dir) so the /MIR mirror never deletes saved reports.
        f'robocopy "{new_root_s}" "{install_s}" /MIR /XD "{install_s}\\Reports" '
        '/R:5 /W:2 /NFL /NDL /NJH /NJS >> "%LOG%" 2>&1\r\n'
        "set RC=%ERRORLEVEL%\r\n"
        'echo robocopy exit code %RC%>> "%LOG%"\r\n'
        "if %RC% GEQ 8 (\r\n"
        '    echo Update FAILED ^(robocopy %RC%^); relaunching unchanged install.>> "%LOG%"\r\n'
        f'    start "" "{install_s}\\{exe_name}"\r\n'
        "    exit /b 1\r\n"
        ")\r\n"
        'echo Update installed. Restarting PingPair...>> "%LOG%"\r\n'
        f'start "" "{install_s}\\{exe_name}"\r\n'
        f'rmdir /s /q "{new_root_s}" >nul 2>&1\r\n'
        "endlocal\r\n"
    )


def stage_update(zip_path: Path, *, cache: Path | None = None) -> Path:
    """Extract ``zip_path`` to a fresh staging folder; return the bundle root.

    The returned path is the directory that directly contains ``EXE_NAME``,
    ready to be mirrored over the install folder by the swap helper.
    """
    cache = cache or update_cache_dir()
    staging = cache / "staging"
    _rmtree_quietly(staging)
    extract_zip(zip_path, staging)
    return locate_bundle_root(staging)


def apply_update(zip_path: Path, *, cache: Path | None = None) -> None:
    """Stage ``zip_path`` and launch the detached swap helper.

    On a frozen build this returns after the helper is launched; the caller
    must then quit the app promptly so the helper can take the file locks.
    Raises :class:`SourceInstallError` on a non-frozen checkout and
    :class:`UpdateApplyError` on any staging/launch failure.
    """
    if not is_frozen():
        raise SourceInstallError(
            "PingPair is running from source. Update it with 'git pull' "
            "instead — the in-app updater only replaces packaged builds."
        )
    cache = cache or update_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    new_root = stage_update(zip_path, cache=cache)
    target = install_dir()
    log_path = cache / "update_log.txt"
    script = build_swap_script(os.getpid(), new_root, target, log_path=log_path)
    script_path = cache / "apply_update.cmd"
    script_path.write_text(script, encoding="ascii")
    # SEC-002: the staged build + this helper sit in a per-user-writable dir but
    # are about to be copied into the install by an ELEVATED helper. Lock them to
    # Administrators only so a non-admin local process can't swap them in the
    # window before the copy (CWE-377 TOCTOU). Best-effort; never blocks the
    # update.
    _restrict_to_admins(cache)
    _launch_detached(script_path)


def _append_update_log(cache_dir: Path, line: str) -> None:
    """Append one diagnostic line to the update log; never raise.

    Shares ``update_log.txt`` with the swap helper so a failed icacls
    hardening (which doesn't raise and previously left no trace) is visible
    next to the robocopy transcript.
    """
    try:
        log = cache_dir / "update_log.txt"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError:
        pass


def _restrict_to_admins(path: Path) -> None:
    """Best-effort: lock ``path`` (recursively) to Administrators + SYSTEM only.

    The update is staged in a per-user-writable dir but then copied into the
    install by an **elevated** helper, so a non-admin local process could swap
    the staged files (or the helper script) in the window before the copy runs
    — a TOCTOU local-privilege-escalation vector (CWE-377). PingPair always runs
    elevated, so it can still read/write the staging area; this just keeps
    non-admins out. Best-effort: a failure (non-NTFS, locale, missing
    ``icacls``) leaves the prior behaviour and never raises. Windows-only.

    Well-known SIDs keep it locale-independent: ``*S-1-5-32-544`` is the
    Administrators group, ``*S-1-5-18`` is SYSTEM.
    """
    if sys.platform != "win32":
        return
    import subprocess

    try:
        proc = subprocess.run(
            harden_argv(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-5-32-544:(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                    "/T",
                    "/C",
                    "/Q",
                ]
            ),
            creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            # icacls ran but didn't apply the ACL (non-NTFS, locale, locked
            # file). The staging dir is then NOT admin-locked — surface it in
            # the update log so a failed hardening is diagnosable rather than
            # silently assumed-applied.
            _append_update_log(
                path,
                f"WARNING: icacls hardening returned {proc.returncode}; "
                f"staging dir may not be admin-locked. "
                f"{(proc.stderr or '').strip()[:200]}",
            )
    except (OSError, subprocess.SubprocessError):
        pass


def _launch_detached(script_path: Path) -> None:
    """Start the swap ``.cmd`` **hidden**, surviving our exit.

    ``CREATE_NO_WINDOW`` runs the helper with a console (so ``ping`` /
    ``robocopy`` / ``find`` behave) but **no visible window** — the user asked
    for a silent install, and the GUI's "PingPair will now close and reopen"
    dialog already provides the feedback. Because nothing is visible, the script
    must never block on input (it has no ``pause`` — see :func:`build_swap_script`).
    ``CREATE_NEW_PROCESS_GROUP`` + ``close_fds`` keep it alive after this
    process quits and shield it from a stray Ctrl-C.
    """
    import subprocess

    flags = 0
    if sys.platform == "win32":
        flags = (
            subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    try:
        subprocess.Popen(
            harden_argv(["cmd", "/c", str(script_path)]),
            creationflags=flags,
            close_fds=True,
            cwd=str(script_path.parent),
        )
    except OSError as exc:
        raise UpdateApplyError(f"Could not launch the updater ({exc}).") from exc


def clear_update_cache(*, cache: Path | None = None) -> None:
    """Delete the per-user update scratch dir (downloads + staging).

    Called after a cancelled/failed download and on a fresh download start so a
    partial or stale bundle never piles up in ``%APPDATA%\\PingPair\\update``.
    Best-effort: a locked file (e.g. the swap helper still finishing) is
    skipped rather than raising. NEVER touches the install folder.
    """
    import shutil

    target = cache or update_cache_dir()
    shutil.rmtree(target, ignore_errors=True)


def _rmtree_quietly(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise UpdateApplyError(
            f"Could not clear the staging folder {path} ({exc})."
        ) from exc
