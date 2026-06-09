"""Theme logic (Qt-free): mode resolution, coercion, palette completeness.

The QPalette/QSS application path needs a running QApplication and is
verified by manual/headless render rather than here. These cover the
pure decision logic and the colour-spec invariants.
"""

import re

from pingpair.theme import PALETTES, ThemeMode, resolve_effective

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def test_system_follows_os() -> None:
    assert resolve_effective(ThemeMode.SYSTEM, system_is_dark=True) == "dark"
    assert resolve_effective(ThemeMode.SYSTEM, system_is_dark=False) == "light"


def test_explicit_modes_ignore_os() -> None:
    assert resolve_effective(ThemeMode.DARK, system_is_dark=False) == "dark"
    assert resolve_effective(ThemeMode.LIGHT, system_is_dark=True) == "light"


def test_resolve_accepts_raw_strings() -> None:
    assert resolve_effective("dark", system_is_dark=False) == "dark"
    assert resolve_effective("system", system_is_dark=True) == "dark"


def test_unknown_mode_follows_os_like_system() -> None:
    # Defensive: a corrupt stored value behaves like "system" (coerce()
    # already maps unknowns to SYSTEM before this is ever reached).
    assert resolve_effective("nonsense", system_is_dark=True) == "dark"
    assert resolve_effective("nonsense", system_is_dark=False) == "light"


def test_coerce_known_and_unknown() -> None:
    assert ThemeMode.coerce("dark") is ThemeMode.DARK
    assert ThemeMode.coerce("light") is ThemeMode.LIGHT
    assert ThemeMode.coerce("system") is ThemeMode.SYSTEM
    assert ThemeMode.coerce("") is ThemeMode.SYSTEM
    assert ThemeMode.coerce(None) is ThemeMode.SYSTEM


def test_light_and_dark_define_the_same_roles() -> None:
    assert set(PALETTES["dark"]) == set(PALETTES["light"])


def test_all_palette_values_are_hex() -> None:
    for spec in PALETTES.values():
        for role, value in spec.items():
            assert _HEX.match(value), f"{role}={value!r}"
