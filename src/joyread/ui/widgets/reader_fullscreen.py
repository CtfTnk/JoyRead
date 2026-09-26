"""Reader-only full-screen keyboard behavior shared by both window hosts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import QWidget

from joyread.ui.widgets.window_state import is_maximized


_RESTORE_MAXIMIZED = "_joyread_reader_restore_maximized"


def enter_reader_fullscreen(window: QWidget) -> None:
    if window.isFullScreen():
        return
    # Qt's isMaximized() can lose a compositor-driven zoom on macOS.
    maximized = is_maximized(window)
    setattr(window, _RESTORE_MAXIMIZED, maximized)
    if maximized and QGuiApplication.platformName() == "windows":
        # Native Qt on Windows drops a frameless translucent window from
        # maximized to normal on the first showFullScreen() call, while already
        # resizing it to screen geometry. Clear maximized first so one F press
        # enters the real full-screen state; the saved flag restores it on Esc.
        window.showNormal()
    window.showFullScreen()


def leave_reader_fullscreen(window: QWidget) -> bool:
    if not window.isFullScreen():
        return False
    maximized = bool(getattr(window, _RESTORE_MAXIMIZED, False))
    if maximized:
        window.showMaximized()
    else:
        window.showNormal()
    return True


def handle_reader_fullscreen_key(event: QKeyEvent, window: QWidget) -> bool:
    if event.modifiers() != Qt.KeyboardModifier.NoModifier:
        return False
    if event.key() == Qt.Key.Key_F:
        enter_reader_fullscreen(window)
    elif event.key() == Qt.Key.Key_Escape and window.isFullScreen():
        leave_reader_fullscreen(window)
    else:
        return False
    event.accept()
    return True
