"""Round-8 #2: the Config tab's 'Live CLI preview' pane was removed.

It duplicated what the form + Raw JSON pane already show and only ate vertical
space (and kept collapsing to ~1 line). These guards lock the removal so it
can't silently creep back, and confirm the tab still builds without it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="ConfigView is a Qt widget")

from PySide6.QtWidgets import QApplication, QGroupBox

from pingpair.config import load_default_config
from pingpair.context import AppContext
from pingpair.views.config_view import ConfigView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _config_view(qapp) -> ConfigView:
    return ConfigView(AppContext.create(load_default_config()))


def test_config_tab_builds_without_preview(qapp):
    view = _config_view(qapp)
    # The preview widget + its builder are gone.
    assert not hasattr(view, "_preview")
    assert not hasattr(view, "_refresh_preview")


def test_no_live_cli_preview_group_box(qapp):
    view = _config_view(qapp)
    titles = {b.title() for b in view.findChildren(QGroupBox)}
    assert "Live CLI preview" not in titles
    # The panes that stay are still present.
    assert "Raw JSON" in titles
