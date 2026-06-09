"""Quick Start ↔ Welcome screenshot parity (2026-06-02).

The generated Quick Start section must show the SAME per-role screenshots as
the welcome tour: each Setup step pinned to Server / Client / Loopback, the Run
+ Save steps pinned to Client — regardless of the running role. Before the fix,
``help_view`` resolved every figure against the *running* role, so e.g. in
Loopback mode all three Setup steps showed the Loopback shot and Run/Save showed
Loopback instead of Client. This guards the role-qualified resolution path
(``_shots_theme_root`` + the generator's ``<role>/<tab>/<file>`` srcs).
"""
from __future__ import annotations

import logging

import pytest

from pingpair.config import load_default_config
from pingpair.context import AppContext, Role, RunState

# (role-qualified src as emitted by the Quick Start generator) -> pinned role.
PINNED = {
    "server/setup/01-checks-overview.png": "server",
    "client/setup/01-checks-overview.png": "client",
    "loopback/setup/01-checks-overview.png": "loopback",
    "client/run/01-overview.png": "client",
    "client/save-options/02-finish-popup.png": "client",
}


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="HelpView is a Qt widget")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _help_view_in_role(qapp, role: Role):
    from pingpair.views.help_view import HelpView

    ctx = AppContext(
        config=load_default_config(),
        logger=logging.getLogger("test-qs-parity"),
        run_state=RunState(role=role),
    )
    view = HelpView(ctx)
    view._jump_to_key("quick-start")  # render the Quick Start section
    return view


def test_quick_start_pins_each_step_to_its_role_not_running_role(qapp) -> None:
    # Running role = Loopback, yet the role-pinned steps still resolve to THEIR
    # own role's capture (Server / Client) — proving the per-card pin wins over
    # the running role (the bug task 2 fixed).
    view = _help_view_in_role(qapp, Role.LOOPBACK)
    for src, role in PINNED.items():
        path = view._resolve_shot_path(src)
        assert path is not None, f"Quick Start figure {src!r} did not resolve"
        assert path.is_file(), f"{src!r} resolved to a non-file {path}"
        assert role in path.parts, f"{src!r} resolved to {path}, expected role {role!r}"


def test_quick_start_html_carries_role_qualified_srcs() -> None:
    # The committed Quick Start HTML must carry the role-qualified srcs (so it
    # matches the welcome tour screenshot-for-screenshot, not the running role).
    from pingpair.help_loader import list_sections
    from pingpair.paths import HELP_DIR

    qs = {s.key: s for s in list_sections(HELP_DIR)}["quick-start"]
    html = qs.index_path.read_text(encoding="utf-8")
    for src in PINNED:
        assert f'src="{src}"' in html, f"Quick Start missing role-pinned {src!r}"
