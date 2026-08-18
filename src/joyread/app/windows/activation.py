"""Qt-free most-recently-activated tracking and the dock reopen decision.

macOS sends a reopen Apple Event when the user clicks a running application in
the Dock. The platform convention is that reopen *activates* what is already
there and only creates a window when the application has none. JoyRead
additionally has no single "document window" to fall back on, so it restores
whatever the user touched last.
"""

from __future__ import annotations

from enum import StrEnum


class ReopenDecision(StrEnum):
    """What a reopen request should do to the current window set."""

    ACTIVATE_RECENT = "activate_recent"
    SHOW_LIBRARY = "show_library"


class WindowActivationRegistry:
    """Remember activation order so reopen can restore the user's last window.

    Keys are opaque and caller-supplied. Re-recording a known key moves it to
    the front rather than duplicating it.
    """

    def __init__(self) -> None:
        self._order: list[object] = []

    def record_activation(self, key: object) -> None:
        self.forget(key)
        self._order.insert(0, key)

    def forget(self, key: object) -> None:
        self._order = [candidate for candidate in self._order if candidate != key]

    def most_recent(self) -> object | None:
        return self._order[0] if self._order else None

    def ordered(self) -> tuple[object, ...]:
        return tuple(self._order)

    def __len__(self) -> int:
        return len(self._order)


def decide_reopen(registry: WindowActivationRegistry) -> ReopenDecision:
    """Choose between raising the last-used window and building the Library."""

    if registry.most_recent() is None:
        return ReopenDecision.SHOW_LIBRARY
    return ReopenDecision.ACTIVATE_RECENT
