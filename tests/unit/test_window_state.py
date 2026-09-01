"""Unit tests for the window's maximized state and remembered restore size."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMainWindow

from joyread.ui.widgets import window_state
from joyread.ui.widgets.window_state import (
    fills_the_screen,
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


def _cocoa(monkeypatch, on: bool = True) -> None:
    """Pin the macOS *behaviour*, without touching the AppKit safety gate.

    Patching ``on_cocoa`` itself would also let ``_appkit_is_zoomed`` reach for
    an NSWindow that an offscreen window does not have, which segfaults.
    """
    monkeypatch.setattr(window_state, "_platform_forgets_the_restore_size", lambda: on)
    monkeypatch.setattr(window_state, "_geometry_alone_leaves_maximized", lambda: on)


# --- the remembered restore size -------------------------------------------


def test_the_remembered_size_survives_a_maximize_animation(qtbot, monkeypatch) -> None:
    """The whole point of owning it.

    A platform maximize is animated, and Qt records every intermediate frame as
    the new ``normalGeometry()``. Measured on macOS: dragging to the top edge
    walks a 900x600 window through 19 frames to 1512x949 over ~350ms, and Qt's
    memory of the size to restore to ends up being the maximized size. A
    latched value must not move when frames arrive.
    """
    _cocoa(monkeypatch)
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
    _cocoa(monkeypatch)
    window = _window(qtbot)

    assert restore_geometry(window) == window.normalGeometry()


def test_a_maximized_window_has_no_size_worth_remembering(qtbot, monkeypatch) -> None:
    _cocoa(monkeypatch)
    window = _window(qtbot)
    remember_restore_geometry(window)
    chosen = restore_geometry(window)

    window.showMaximized()
    qtbot.wait(20)
    remember_restore_geometry(window)

    assert restore_geometry(window) == chosen, "latching while maximized would store full screen"


def test_each_window_remembers_its_own_size(qtbot, monkeypatch) -> None:
    _cocoa(monkeypatch)
    first = _window(qtbot)
    second = _window(qtbot)
    second.resize(500, 400)
    qtbot.wait(10)

    remember_restore_geometry(first)
    remember_restore_geometry(second)

    assert restore_geometry(first).size() != restore_geometry(second).size()


def test_latching_twice_keeps_the_later_size(qtbot, monkeypatch) -> None:
    _cocoa(monkeypatch)
    window = _window(qtbot)
    remember_restore_geometry(window)
    window.resize(700, 500)
    qtbot.wait(10)
    remember_restore_geometry(window)

    assert restore_geometry(window).size() == QRect(0, 0, 700, 500).size()


def test_a_platform_that_remembers_for_us_is_left_alone(qtbot, monkeypatch) -> None:
    # Off macOS ``normalGeometry()`` survives a maximize, and second-guessing it
    # would be a behaviour change on a platform none of this was measured on.
    _cocoa(monkeypatch, on=False)
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
    monkeypatch.setattr(window_state, "_appkit_is_zoomed", lambda window: True)
    window = _window(qtbot)

    assert window.isMaximized() is False, "precondition: the widget flag disagrees"
    assert is_maximized(window) is True


def test_an_unreachable_platform_falls_back_to_qt(qtbot, monkeypatch) -> None:
    # None, not False: an NSWindow we cannot read is not evidence the window is
    # unmaximized.
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

    monkeypatch.setattr(window_state, "on_cocoa", lambda: True)
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
    _cocoa(monkeypatch)
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
    _cocoa(monkeypatch)
    _cocoa(monkeypatch, on=False)
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
    _cocoa(monkeypatch, on=False)
    window = _window(qtbot)
    window.showMaximized()
    qtbot.wait(20)

    leave_maximized(window, geometry=QRect())
    qtbot.wait(20)

    assert not (
        window.windowHandle().windowStates() & Qt.WindowState.WindowMaximized
    ), "the platform window, not just the widget, must have left maximized state"


def test_every_behaviour_predicate_follows_the_backend(monkeypatch) -> None:
    """One source of truth, and it is the QPA backend rather than the OS.

    macOS running the offscreen plugin behaves the X11 way for all of this, so
    a predicate keyed off ``sys.platform`` would switch on workarounds that are
    wrong there. Driven from both sides so a predicate hard-coded either way is
    caught on any machine.
    """
    predicates = (
        window_state._platform_forgets_the_restore_size,
        window_state._geometry_alone_leaves_maximized,
    )
    for backend in (True, False):
        monkeypatch.setattr(window_state, "on_cocoa", lambda backend=backend: backend)
        for predicate in predicates:
            assert predicate() is backend, predicate.__name__


def test_the_backend_gate_reads_the_qpa_plugin() -> None:
    assert window_state.on_cocoa() is (QGuiApplication.platformName() == "cocoa")


# --- only shrink what is actually big ---------------------------------------


def _looks_maximized(monkeypatch) -> None:
    """Make the platform claim the window is maximized, whatever its size."""
    monkeypatch.setattr(window_state, "_appkit_is_zoomed", lambda window: True)


def test_a_window_filling_the_screen_is_still_restored(qtbot) -> None:
    window = _window(qtbot)
    window.showMaximized()
    qtbot.wait(20)

    assert fills_the_screen(window), "a maximized window fills its screen"


def test_a_window_the_user_resized_is_left_at_that_size(qtbot, monkeypatch) -> None:
    """macOS keeps its own resize edge on a tiled window.

    That edge bypasses our resize grips entirely -- ``begin_system_resize``
    refuses while maximized, and it makes no difference, because the drag never
    reaches us. AppKit resizes the tile and leaves it tiled, so the window is
    still ``isZoomed`` at a size the user chose deliberately. Restoring it then
    discards that size, which is what the report described: resize a zoomed
    window to 800x900, drag it, and it snaps back to the size from before.
    """
    _looks_maximized(monkeypatch)
    window = _window(qtbot)
    window.resize(800, 900)
    qtbot.wait(10)

    assert is_maximized(window), "precondition: the platform still calls it maximized"
    assert not fills_the_screen(window), "but it is no longer filling the screen"


def test_a_few_pixels_of_inset_still_counts_as_filling(qtbot, monkeypatch) -> None:
    # Exact equality would be brittle: a platform that insets a maximized frame
    # by a pixel would stop restoring altogether, which is the worse failure.
    window = _window(qtbot)
    available = window.screen().availableGeometry().size()
    window.resize(available.width() - 2, available.height() - 2)
    qtbot.wait(10)

    assert fills_the_screen(window)


def test_no_screen_falls_back_to_restoring(qtbot, monkeypatch) -> None:
    # Unable to tell, so trust the caller's reading of the window state rather
    # than silently skipping a restore it wanted.
    window = _window(qtbot)
    monkeypatch.setattr(type(window), "screen", lambda self: None)

    assert fills_the_screen(window) is True


# --- the ways out when there is nowhere to put the window -------------------


class _Recorder(QObject):
    """Records the calls the restore path makes, in order."""

    def __init__(self, *, maximized: bool = False) -> None:
        super().__init__()
        self._maximized = maximized
        self.calls: list[str] = []

    def isMaximized(self) -> bool:  # noqa: N802 - Qt API shape.
        return self._maximized

    def isFullScreen(self) -> bool:  # noqa: N802 - Qt API shape.
        return False

    def windowHandle(self):  # noqa: N802 - Qt API shape.
        return None

    def geometry(self) -> QRect:
        return QRect(0, 0, 1512, 949)

    def normalGeometry(self) -> QRect:  # noqa: N802 - Qt API shape.
        return QRect(100, 100, 900, 600)

    def showNormal(self) -> None:  # noqa: N802 - Qt API shape.
        self.calls.append("showNormal")

    def setGeometry(self, *args) -> None:  # noqa: N802 - Qt API shape.
        self.calls.append("setGeometry")


def test_an_invalid_target_leaves_maximized_without_placing_the_window(monkeypatch) -> None:
    """Pinned in the Cocoa shape, which is the one that can go wrong.

    There ``showNormal()`` is otherwise skipped, so dropping the guard would
    leave ``setGeometry(QRect())`` as the only call -- placing the window
    nowhere and never taking it out of maximized state.
    """
    monkeypatch.setattr(window_state, "_geometry_alone_leaves_maximized", lambda: True)
    window = _Recorder()

    leave_maximized(window, geometry=QRect())

    assert window.calls == ["showNormal"], "the only way out when there is no rectangle"


def test_a_window_with_no_nswindow_is_not_evidence_either(qtbot, monkeypatch) -> None:
    # Same reasoning as an unreadable one: absence of an NSWindow is not proof
    # the window is unmaximized.
    monkeypatch.setattr(window_state, "on_cocoa", lambda: True)
    monkeypatch.setattr(window_state, "_native_window", lambda window: None)
    window = _window(qtbot)

    assert window_state._appkit_is_zoomed(window) is None


def test_the_zoom_button_lets_a_remembering_platform_place_the_window(monkeypatch) -> None:
    """Off Cocoa, ``showNormal()`` restores the position as well as the size.

    Following it with ``setGeometry()`` would fight a window manager that still
    owns the frame, which is the failure mode Linux was already documented for.
    So the button must ask for nothing more than the state change there.
    """
    monkeypatch.setattr(window_state, "_platform_forgets_the_restore_size", lambda: False)
    monkeypatch.setattr(window_state, "_geometry_alone_leaves_maximized", lambda: False)
    window = _Recorder(maximized=True)

    toggle_maximized(window)

    assert window.calls == ["showNormal"], "no client-side placement to fight the WM with"


def test_the_zoom_button_places_the_window_where_the_platform_forgets(monkeypatch) -> None:
    monkeypatch.setattr(window_state, "_platform_forgets_the_restore_size", lambda: True)
    monkeypatch.setattr(window_state, "_geometry_alone_leaves_maximized", lambda: True)
    window = _Recorder(maximized=True)

    toggle_maximized(window)

    assert window.calls == ["setGeometry"], "the size we remembered, in one frame change"
