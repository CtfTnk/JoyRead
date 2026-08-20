"""Input-capturing backdrop for non-modal floating panels.

Replaces manual "was this click inside or outside the panel's rectangle"
geometry checks with ordinary Qt hit-testing: a scrim spans its parent's
full area and sits directly below the currently-open panel in z-order, so
Qt routes any click that isn't on the panel here first.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from joyread.ui.views.window_drag import start_window_drag_if_on_drag_handle


class FloatingPanelScrim(QWidget):
    """Transparent widget that captures input for whatever sits above it.

    Paints nothing, so the content underneath stays fully visible. A click
    anywhere on the scrim is swallowed and dismisses the open panel; a wheel
    event is swallowed only, so scrolling behind an open panel does nothing
    instead of reaching the content underneath. A click on an unoccupied
    part of the drag handle (e.g. the reader header) starts a window move
    instead of dismissing -- the scrim would otherwise sit above the header
    and block dragging whenever a panel is open. Header controls remain
    outside-clicks, not drag targets.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._dismiss: Callable[[], None] | None = None
        self._drag_handle: QWidget | None = None

    def set_dismiss_callback(self, callback: Callable[[], None] | None) -> None:
        self._dismiss = callback

    def set_drag_handle(self, drag_handle: QWidget | None) -> None:
        self._drag_handle = drag_handle

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if start_window_drag_if_on_drag_handle(event, self._drag_handle):
            return
        if self._dismiss is not None:
            self._dismiss()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()


__all__ = ["FloatingPanelScrim"]
