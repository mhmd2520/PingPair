"""settings.reset_all — the Setup-tab app-wide factory reset.

Driven by the "Reset all settings" button on the Setup tab (it replaced
the short-lived Preferences tab). Clears every persisted QSettings key.
"""

from PySide6.QtCore import QSettings

from pingpair import settings


def test_reset_all_clears_every_key(tmp_path, monkeypatch) -> None:
    # Point settings at a throwaway .ini file so the test never touches
    # the real registry / the developer's actual settings.
    ini = tmp_path / "settings.ini"

    def _fake_q() -> QSettings:
        return QSettings(str(ini), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings, "_q", _fake_q)

    seed = _fake_q()
    seed.setValue("role/value", "server")
    seed.setValue("report/dir", "X:/somewhere")
    seed.setValue("window/active_tab", "3")
    seed.sync()

    settings.reset_all()

    after = _fake_q()
    assert after.value("role/value") is None
    assert after.value("report/dir") is None
    assert after.value("window/active_tab") is None
