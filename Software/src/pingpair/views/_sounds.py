"""Shared notification-sound helper for the view layer (Round-6 #7, Round-7 #2).

Bridges the Qt-free, settings-free :mod:`pingpair.core.sounds` to the GUI by
reading the persisted "Play notification sounds" preference and delegating.
One place so the Run tab (sweep events) and the About tab (update pop-ups) can't
drift on how the toggle is honoured.
"""

from __future__ import annotations

from .. import settings
from ..core.sounds import SoundEvent, play

__all__ = ["SoundEvent", "notify_sound"]


def notify_sound(event: SoundEvent) -> None:
    """Play the notification sound for ``event``, honouring the Setup-tab toggle.

    Reads the preference each call (events are infrequent) and delegates to the
    best-effort :func:`pingpair.core.sounds.play`, which is a quiet no-op when
    sounds are off or unavailable."""
    play(event, enabled=settings.load_sounds_enabled())
