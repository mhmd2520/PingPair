"""Guard where run reports resolve (Phase-5 packaging, #7).

Frozen builds must write reports BESIDE the .exe (``<install>/Reports``), not
inside ``_internal/`` — both so users find them next to PingPair.exe and so the
self-update's ``robocopy /MIR`` of ``_internal`` can't wipe them.
"""

from __future__ import annotations

from pathlib import Path

from pingpair import paths


def test_reports_root_dev_is_under_software(monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", False, raising=False)
    root = paths._reports_root()
    assert root == paths.PROJECT_ROOT / "Reports"


def test_reports_root_frozen_is_beside_exe(monkeypatch, tmp_path):
    exe = tmp_path / "install" / "PingPair.exe"
    exe.parent.mkdir(parents=True)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(exe), raising=False)
    root = paths._reports_root()
    # Beside the exe, NOT inside an _internal folder.
    assert root == exe.parent / "Reports"
    assert "_internal" not in Path(root).parts
