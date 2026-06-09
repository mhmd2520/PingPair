"""Optional notification sounds for key user-facing events (Round-6 #7).

Qt-free, dependency-free, and Windows-first: plays the user's own **Windows
system event sounds** via :mod:`winsound` (``MessageBeep``), so PingPair
respects whatever sound scheme the user has configured in Windows and ships
**no audio assets of its own**. On non-Windows platforms (dev only) it is a
quiet no-op.

The view layer calls :func:`play` at a handful of moments — a sweep finishing
OK / with failed cases, a sweep aborting, and a dialog asking the operator to
choose — passing the current ``enabled`` preference (persisted in QSettings,
toggled on the Setup tab's Appearance box). Keeping the enable flag a
*parameter* rather than module state keeps this module pure and trivially
testable; the optional ``beeper`` injection lets tests assert the event→tone
mapping without actually making noise.

Why system sounds, not bundled ``.wav`` files: zero shipped assets, no extra Qt
multimedia module to bundle (which previously bit the frozen build, see the
QtOpenGL spec note), and the sounds match the rest of the user's OS. Swapping
to custom audio later means changing only this module.
"""

from __future__ import annotations

import enum
import sys
from collections.abc import Callable


class SoundEvent(enum.Enum):
    """A user-facing moment worth an audible cue."""

    SUCCESS = "success"   # a sweep / multi-segment run finished cleanly
    FAILURE = "failure"   # a sweep finished, but some cases failed
    ERROR = "error"       # a sweep aborted / a hard error popup
    PROMPT = "prompt"     # a dialog is asking the operator to decide


# Each event → a Windows ``MessageBeep`` type. These play the user's own
# configured system sounds (Asterisk / Critical Stop / Exclamation), so the app
# inherits the OS sound scheme rather than bundling its own. Values are the
# literal MB_* flags so the table needs no winsound import (keeps the module
# importable — and testable — on non-Windows).
MB_ICONHAND = 0x10         # Critical Stop
MB_ICONEXCLAMATION = 0x30  # Exclamation
MB_ICONASTERISK = 0x40     # the gentle "ding" (Information / Asterisk)

_WINDOWS_BEEP: dict[SoundEvent, int] = {
    SoundEvent.SUCCESS: MB_ICONASTERISK,
    SoundEvent.FAILURE: MB_ICONHAND,
    SoundEvent.ERROR: MB_ICONHAND,
    SoundEvent.PROMPT: MB_ICONEXCLAMATION,
}


def play(
    event: SoundEvent,
    *,
    enabled: bool = True,
    beeper: Callable[[int], None] | None = None,
) -> bool:
    """Play the notification sound for ``event`` when ``enabled``.

    Returns True iff a sound was actually issued (enabled + a known event +
    a usable backend). Never raises — audio is best-effort: a disabled
    preference, a non-Windows host, a missing ``winsound``, a headless
    session, or a sound-device error all fall through to a quiet no-op so a
    beep can never break the app flow.

    ``beeper`` (a ``MessageBeep``-shaped callable) is injectable so tests can
    assert the event→tone mapping without making noise; production passes
    nothing and the function resolves ``winsound.MessageBeep`` itself.
    """
    if not enabled:
        return False
    tone = _WINDOWS_BEEP.get(event)
    if tone is None:
        return False
    if beeper is None:
        if sys.platform != "win32":
            return False
        try:
            import winsound

            beeper = winsound.MessageBeep
        except Exception:  # noqa: BLE001 — audio must never break the app
            return False
    try:
        beeper(tone)
        return True
    except Exception:  # noqa: BLE001 — a failed beep is non-fatal
        return False
