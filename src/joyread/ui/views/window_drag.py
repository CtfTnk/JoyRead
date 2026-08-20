"""Let a full-window overlay's own drag handle move the window.

Dialogs, floating panels, and the settings page all span the whole window
(or shell) and swallow every mouse press to stay modal -- which also
swallows presses meant to drag the window via its title bar/header, since
those overlays sit above it. This lets each of them recognize a press
landing on that drag handle and hand it to the OS instead of treating it
as a dismiss click.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget


def start_window_drag_if_on_drag_handle(event: QMouseEvent, drag_handle: QWidget | None) -> bool:
    """Recognize, and best-effort start, a window drag from this press.

    Only an unoccupied part of ``drag_handle`` counts as a drag region.
    Overlays sit above title/header controls, so treating every point inside
    the handle as a drag would turn an underlying close, navigation, or panel
    button into a window move. Returning True still means drag intent even
    when ``startSystemMove()`` is unsupported, so callers do not dismiss the
    overlay in that case.
    """

    if drag_handle is None or event.button() != Qt.MouseButton.LeftButton:
        return False
    local = drag_handle.mapFromGlobal(event.globalPosition().toPoint())
    if not drag_handle.rect().contains(local):
        return False
    if drag_handle.childAt(local) is not None:
        return False
    window_handle = drag_handle.window().windowHandle()
    if window_handle is not None:
        window_handle.startSystemMove()
    return True


__all__ = ["start_window_drag_if_on_drag_handle"]
