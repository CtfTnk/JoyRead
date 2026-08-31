"""Unit tests for the window's maximized state and remembered restore size."""

from __future__ import annotations

import sys

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QMainWindow

from joyread.ui.widgets import window_state
from joyread.ui.widgets.window_state import (
    is_maximized,
    leave_maximized,
    remember_restore_geometry,
    restore_geometry,
    toggle_maximized,
)


def _window(qtbot) -> QMainWindow:
    window = QMainWindow()
    qtbot.addWidget(window)
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.resize(900, 600)
    window.show()
    qtbot.wait(10)
    return window


def _owns_the_size(monkeypatch) -> None:
    """Pin the macOS shape: the platform forgets, so we remember."""
    monkeypatch.setattr(window_state, "_PLATFORM_FORGETS_THE_RESTORE_SIZE", True)


# --- the remembered restore size -------------------------------------------


def test_the_remembered_size_survives_a_maximize_animation(qtbot, monkeypatch) -> None:
    """The whole point of owning it.

    A platform maximize is animated, and Qt records every intermediate frame as
    the new ``normalGeometry()``. Measured on macOS: dragging to the top edge
    walks a 900x600 window through 19 frames to 1512x949 over ~350ms, and Qt's
    memory of the size to restore to ends up being the maximized size. A
    latched value must not move when frames arrive.
    """
    _owns_the_size(monkeypatch)
    window = _window(qtbot)
    remember_restore_geometry(window)

    for width, height in ((930, 617), (1070, 697), (1326, 843), (1512, 949)):
        window.resize(width, height)
        qtbot.wait(2)

    assert restore_geometry(window).size() == QRect(0, 0, 900, 600).size(), (
        "the size the user chose must survive the frames the animation delivers"
    )


def test_nothing_latched_yet_falls_back_to_qt(qtbot, monkeypatch) -> None:
    # A window maximized before it was ever dragged or resized. Qt's answer is
    # wrong only *after* an animation, so it is still the best guess here.
    _owns_the_size(monkeypatch)
    window = _window(qtbot)

    assert restore_geometry(window) == window.normalGeometry()


def test_a_maximized_window_has_no_size_worth_remembering(qtbot, monkeypatch) -> None:
    _owns_the_size(monkeypatch)
    window = _window(qtbot)
    remember_restore_geometry(window)
    chosen = restore_geometry(window)

    window.showMaximized()
    qtbot.wait(20)
    remember_restore_geometry(window)

    assert restore_geometry(window) == chosen, "latching while maximized would store full screen"


def test_each_window_remembers_its_own_size(qtbot, monkeypatch) -> None:
    _owns_the_size(monkeypatch)
    first = _window(qtbot)
    second = _window(qtbot)
    second.resize(500, 400)
    qtbot.wait(10)

    remember_restore_geometry(first)
    remember_restore_geometry(second)

    assert restore_geometry(first).size() != restore_geometry(second).size()


def test_latching_twice_keeps_the_later_size(qtbot, monkeypatch) -> None:
    _owns_the_size(monkeypatch)
    window = _window(qtbot)
    remember_restore_geometry(window)
    window.resize(700, 500)
    qtbot.wait(10)
    remember_restore_geometry(window)

    assert restore_geometry(window).size() == QRect(0, 0, 700, 500).size()


def test_a_platform_that_remembers_for_us_is_left_alone(qtbot, monkeypatch) -> None:
    # Off macOS ``normalGeometry()`` survives a maximize, and second-guessing it
    # would be a behaviour change on a platform none of this was measured on.
    monkeypatch.setattr(window_state, "_PLATFORM_FORGETS_THE_RESTORE_SIZE", False)
    window = _window(qtbot)
    remember_restore_geometry(window)
    window.resize(640, 480)
    qtbot.wait(10)

    assert restore_geometry(window) == window.normalGeometry()


# --- who is maximized ------------------------------------------------------


def test_the_platform_answer_is_used_when_it_can_be_reached(qtbot, monkeypatch) -> None:
    """AppKit is the only record that survives a platform-initiated zoom.

    ``QWidget.isMaximized()`` is cleared by ``setGeometry()`` without the
    platform being told, so it reads False for a window the window manager
    still has maximized -- which is what let a tiled window get stuck: the
    code stopped trying to restore it while AppKit went on refusing to move it.
    """
    monkeypatch.setattr(window_state, "_PLATFORM_OWNS_THE_ZOOM_STATE", True)
    monkeypatch.setattr(window_state, "_appkit_is_zoomed", lambda window: True)
    window = _window(qtbot)

    assert window.isMaximized() is False, "precondition: the widget flag disagrees"
    assert is_maximized(window) is True


def test_an_unreachable_platform_falls_back_to_qt(qtbot, monkeypatch) -> None:
    # None, not False: an NSWindow we cannot read is not evidence the window is
    # unmaximized.
    monkeypatch.setattr(window_state, "_PLATFORM_OWNS_THE_ZOOM_STATE", True)
    monkeypatch.setattr(window_state, "_appkit_is_zoomed", lambda window: None)
    window = _window(qtbot)
    window.showMaximized()
    qtbot.wait(20)

    assert is_maximized(window) is True


def test_an_unreadable_nswindow_is_not_evidence_of_anything(qtbot, monkeypatch) -> None:
    # Returning False here would strand a maximized window exactly the way the
    # widget flag does, so the failure path must say "no answer", not "no".
    def boom(window):
        raise RuntimeError("no NSWindow")

    monkeypatch.setattr(window_state, "_on_cocoa", lambda: True)
    monkeypatch.setattr(window_state, "_native_window", boom)
    window = _window(qtbot)

    assert window_state._appkit_is_zoomed(window) is None


def test_appkit_is_never_touched_off_the_cocoa_backend(qtbot) -> None:
    """Gated on the QPA plugin, not the OS -- and it must be, not merely should.

    Under the offscreen and minimal plugins ``winId()`` is not an NSView
    pointer, and handing it to ``objc_object()`` segfaults rather than raising,
    straight past the surrounding except. This suite forces offscreen, so a
    regression here takes the whole run down with it.
    """
    window = _window(qtbot)

    assert window_state._appkit_is_zoomed(window) is None


# --- leaving maximized -----------------------------------------------------


def test_toggle_remembers_the_size_before_maximizing(qtbot, monkeypatch) -> None:
    # Latch an older size first, so a toggle that fails to re-latch is visibly
    # different from one that does. Comparing against the current size alone
    # cannot fail: the fallback returns normalGeometry(), which offscreen keeps
    # correct, so it would agree either way.
    _owns_the_size(monkeypatch)
    window = _window(qtbot)
    remember_restore_geometry(window)
    assert restore_geometry(window).size() == QRect(0, 0, 900, 600).size()

    window.resize(760, 540)
    qtbot.wait(10)
    toggle_maximized(window)
    qtbot.wait(20)

    assert is_maximized(window), "precondition: the toggle maximized it"
    assert restore_geometry(window).size() == QRect(0, 0, 760, 540).size(), (
        "the toggle must latch the size the user is leaving, not an older one"
    )


def test_toggle_returns_to_the_remembered_size(qtbot, monkeypatch) -> None:
    _owns_the_size(monkeypatch)
    monkeypatch.setattr(window_state, "_GEOMETRY_ALONE_LEAVES_MAXIMIZED", False)
    window = _window(qtbot)
    window.resize(760, 540)
    qtbot.wait(10)

    toggle_maximized(window)
    qtbot.wait(20)
    toggle_maximized(window)
    qtbot.wait(20)

    assert not is_maximized(window)
    assert window.size() == QRect(0, 0, 760, 540).size()


def test_an_invalid_target_still_leaves_maximized(qtbot, monkeypatch) -> None:
    # Better a window at the wrong size than one stuck full screen.
    monkeypatch.setattr(window_state, "_GEOMETRY_ALONE_LEAVES_MAXIMIZED", False)
    window = _window(qtbot)
    window.showMaximized()
    qtbot.wait(20)

    leave_maximized(window, geometry=QRect())
    qtbot.wait(20)

    assert not (
        window.windowHandle().windowStates() & Qt.WindowState.WindowMaximized
    ), "the platform window, not just the widget, must have left maximized state"


def test_the_traits_are_macos_only() -> None:
    darwin = sys.platform == "darwin"
    assert window_state._PLATFORM_OWNS_THE_ZOOM_STATE is darwin
    assert window_state._PLATFORM_FORGETS_THE_RESTORE_SIZE is darwin
    assert window_state._GEOMETRY_ALONE_LEAVES_MAXIMIZED is darwin
