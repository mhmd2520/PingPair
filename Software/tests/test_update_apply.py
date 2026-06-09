"""Tests for the Feature-6 update applier (staging + swap-script, no Qt).

The detached process launch and the real install-folder swap are not
exercised here (they need a frozen build + a live process); we pin the
deterministic, IO-bounded helpers: zip-slip-safe extraction, bundle-root
location, the swap-script text, and the source-vs-frozen gate.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pingpair.core import update_apply
from pingpair.core.update_apply import (
    EXE_NAME,
    SourceInstallError,
    UpdateApplyError,
    apply_update,
    build_swap_script,
    extract_zip,
    is_frozen,
    locate_bundle_root,
    stage_update,
)


def _make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# --------------------------------------------------------------------------
# extract_zip — safety + correctness
# --------------------------------------------------------------------------


def test_extract_zip_roundtrip(tmp_path):
    z = _make_zip(
        tmp_path / "b.zip",
        {"PingPair/PingPair.exe": b"MZ", "PingPair/_internal/q.dll": b"dll"},
    )
    dest = tmp_path / "out"
    extract_zip(z, dest)
    assert (dest / "PingPair" / "PingPair.exe").read_bytes() == b"MZ"
    assert (dest / "PingPair" / "_internal" / "q.dll").read_bytes() == b"dll"


def test_extract_zip_rejects_path_traversal(tmp_path):
    # A member that escapes the dest via .. must abort the extraction.
    z = _make_zip(tmp_path / "evil.zip", {"../escape.txt": b"pwned"})
    with pytest.raises(UpdateApplyError):
        extract_zip(z, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


# --------------------------------------------------------------------------
# locate_bundle_root
# --------------------------------------------------------------------------


def test_locate_bundle_root_at_top_level(tmp_path):
    (tmp_path / EXE_NAME).write_bytes(b"MZ")
    assert locate_bundle_root(tmp_path) == tmp_path


def test_locate_bundle_root_one_level_deep(tmp_path):
    inner = tmp_path / "PingPair"
    inner.mkdir()
    (inner / EXE_NAME).write_bytes(b"MZ")
    assert locate_bundle_root(tmp_path) == inner


def test_locate_bundle_root_missing_exe_raises(tmp_path):
    (tmp_path / "readme.txt").write_text("no exe here")
    with pytest.raises(UpdateApplyError):
        locate_bundle_root(tmp_path)


# --------------------------------------------------------------------------
# stage_update
# --------------------------------------------------------------------------


def test_stage_update_returns_bundle_root(tmp_path):
    z = _make_zip(
        tmp_path / "b.zip",
        {"PingPair/PingPair.exe": b"MZ", "PingPair/_internal/x": b"y"},
    )
    root = stage_update(z, cache=tmp_path / "cache")
    assert root.name == "PingPair"
    assert (root / EXE_NAME).is_file()


def test_stage_update_clears_previous_staging(tmp_path):
    cache = tmp_path / "cache"
    stale = cache / "staging" / "old.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    z = _make_zip(tmp_path / "b.zip", {"PingPair.exe": b"MZ"})
    stage_update(z, cache=cache)
    assert not stale.exists()  # staging was wiped before re-extract


# --------------------------------------------------------------------------
# build_swap_script
# --------------------------------------------------------------------------


def test_build_swap_script_contains_pid_paths_and_relaunch():
    script = build_swap_script(
        4242, Path(r"C:\staging\PingPair"), Path(r"C:\Apps\PingPair")
    )
    assert "PID eq 4242" in script
    assert r"C:\staging\PingPair" in script
    assert r"C:\Apps\PingPair" in script
    assert r"C:\Apps\PingPair\PingPair.exe" in script
    assert "robocopy" in script
    assert "/MIR" in script
    # CRLF line endings so cmd.exe parses it on Windows.
    assert "\r\n" in script


def test_build_swap_script_waits_for_process_exit():
    script = build_swap_script(99, Path("a"), Path("b"))
    assert ":waitloop" in script
    assert "goto waitloop" in script


def test_build_swap_script_uses_ping_not_timeout():
    # `timeout` reads stdin and fails instantly with no console (the bug that
    # made an earlier helper silently never swap). Delays must use `ping -n`.
    script = build_swap_script(99, Path("a"), Path("b"))
    assert "ping -n" in script
    assert "timeout" not in script


def test_build_swap_script_logs_and_checks_robocopy_exit():
    script = build_swap_script(
        99, Path(r"C:\s\PingPair"), Path(r"C:\i"), log_path=Path(r"C:\i\u.log")
    )
    assert r"C:\i\u.log" in script  # writes a diagnosable log
    assert "GEQ 8" in script  # robocopy >=8 is a real failure


def test_build_swap_script_is_silent_no_blocking_pause():
    # The helper now runs hidden (CREATE_NO_WINDOW), so it must NEVER block on
    # input — a `pause` would hang invisibly forever. On failure it relaunches
    # the unchanged install instead.
    script = build_swap_script(
        99, Path(r"C:\s\PingPair"), Path(r"C:\i"), log_path=Path(r"C:\i\u.log")
    )
    assert "pause" not in script
    # Both the success and failure paths relaunch the exe so the app always
    # comes back up (>= 2 `start` invocations).
    assert script.count('start ""') >= 2
    assert "exit /b 1" in script  # failure path returns, doesn't hang


def test_build_swap_script_excludes_reports_from_mirror():
    # The user's Reports folder lives beside the .exe (in the install dir);
    # /MIR would delete it on every update without an /XD exclusion.
    script = build_swap_script(
        99, Path(r"C:\s\PingPair"), Path(r"C:\Apps\PingPair")
    )
    assert r'/XD "C:\Apps\PingPair\Reports"' in script


def test_clear_update_cache_removes_dir(tmp_path):
    cache = tmp_path / "update"
    (cache / "download").mkdir(parents=True)
    (cache / "download" / "part.zip").write_bytes(b"x")
    (cache / "staging").mkdir()
    update_apply.clear_update_cache(cache=cache)
    assert not cache.exists()


def test_clear_update_cache_missing_dir_is_noop(tmp_path):
    # Never raises when there's nothing to clean (first run / already gone).
    update_apply.clear_update_cache(cache=tmp_path / "nope")


# --------------------------------------------------------------------------
# apply_update gate (source vs frozen)
# --------------------------------------------------------------------------


def test_apply_update_on_source_install_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(update_apply, "is_frozen", lambda: False)
    z = _make_zip(tmp_path / "b.zip", {"PingPair.exe": b"MZ"})
    with pytest.raises(SourceInstallError):
        apply_update(z, cache=tmp_path / "cache")


def test_apply_update_frozen_stages_and_launches(tmp_path, monkeypatch):
    # Pretend we're frozen; install dir is a throwaway; capture the detached
    # launch instead of actually spawning cmd.exe.
    monkeypatch.setattr(update_apply, "is_frozen", lambda: True)
    monkeypatch.setattr(update_apply, "install_dir", lambda: tmp_path / "install")
    launched: list[Path] = []
    monkeypatch.setattr(
        update_apply, "_launch_detached", lambda p: launched.append(p)
    )
    # Don't actually icacls-lock the throwaway cache (SEC-002) — that would
    # strip the non-elevated test process's access to its own tmp dir.
    monkeypatch.setattr(update_apply, "_restrict_to_admins", lambda p: None)
    z = _make_zip(
        tmp_path / "b.zip",
        {"PingPair/PingPair.exe": b"MZ", "PingPair/_internal/x": b"y"},
    )
    cache = tmp_path / "cache"
    apply_update(z, cache=cache)
    script = cache / "apply_update.cmd"
    assert launched == [script]
    assert script.is_file()
    assert "robocopy" in script.read_text()


def test_is_frozen_default_false_in_tests():
    # The test runner is plain Python, never a PyInstaller build.
    assert is_frozen() is False
