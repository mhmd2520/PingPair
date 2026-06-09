"""Guard the app-icon generation (Feature-5 brand / Phase-5 packaging).

The .ico embedded into PingPair.exe must be **multi-resolution** — a single
256x256 slot makes Windows mis-scale it for every view, which is the
"pixelated icon" bug. These tests pin that the writer emits all the standard
sizes via Pillow's ICO encoder.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="icon rendering needs Qt")
pytest.importorskip("PIL", reason="ICO inspection needs Pillow")

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_write_ico_file_embeds_all_sizes(qapp, tmp_path):
    from PIL import Image

    from pingpair.branding import write_ico_file

    dest = tmp_path / "PingPair.ico"
    write_ico_file(dest)
    assert dest.is_file()
    im = Image.open(dest)
    assert im.format == "ICO"
    sizes = set(im.ico.sizes())
    # The big slot (the one that was missing) plus the small ones Windows uses
    # for the taskbar / list views.
    for expected in [(16, 16), (32, 32), (48, 48), (256, 256)]:
        assert expected in sizes, f"{expected} missing from {sorted(sizes)}"


def test_write_ico_file_custom_sizes(qapp, tmp_path):
    from PIL import Image

    from pingpair.branding import write_ico_file

    dest = tmp_path / "small.ico"
    write_ico_file(dest, sizes=(16, 32))
    im = Image.open(dest)
    assert {(16, 16), (32, 32)} <= set(im.ico.sizes())
