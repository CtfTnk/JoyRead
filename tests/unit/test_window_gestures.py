"""Unit tests for compositor-driven window move and resize."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QMainWindow

from joyread.app.app_context import create_app_context
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.reader_window import ReaderWindow
from joyread.ui.widgets import window_gestures
from joyread.ui.widgets.window_gestures import SystemMoveGesture, install_system_resize_border


def _mouse(
    kind: QMouseEvent.Type,
    *,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
    buttons: Qt.MouseButton | None = None,
) -> QMouseEvent:
    at = QPointF(10, 10)
    return QMouseEvent(
        kind,
        at,
        at,
        button,
        buttons if buttons is not None else button,
        Qt.KeyboardModifier.NoModifier,
    )


def _dragging() -> QMouseEvent:
    """A move with the button still held, as arrives during a drag."""
    return _mouse(
        QMouseEvent.Type.MouseMove,
        button=Qt.MouseButton.NoButton,
        buttons=Qt.MouseButton.LeftButton,
    )


def _record_moves(monkeypatch) -> list:
    started: list = []
    monkeypatch.setattr(
        window_gestures,
        "begin_system_move",
        lambda widget: (started.append(widget), True)[1],
    )
    return started


# --- SystemMoveGesture -----------------------------------------------------


def test_press_hands_the_drag_straight_to_the_compositor(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    started = _record_moves(monkeypatch)
    widget = QMainWindow()
    qtbot.addWidget(widget)
    gesture = SystemMoveGesture()

    assert gesture.press(widget, _mouse(QMouseEvent.Type.MouseButtonPress)) is True
    assert started == [widget]


def test_windows_defers_the_drag_to_the_first_move(qtbot, monkeypatch) -> None:
    # Windows runs the system-move loop synchronously from the press and eats
    # the release Qt needs for double-click-to-zoom, so the press only arms.
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", True)
    started = _record_moves(monkeypatch)
    widget = QMainWindow()
    qtbot.addWidget(widget)
    gesture = SystemMoveGesture()

    assert gesture.press(widget, _mouse(QMouseEvent.Type.MouseButtonPress)) is False
    assert started == []

    assert gesture.move(widget, _mouse(QMouseEvent.Type.MouseMove)) is True
    assert started == [widget]


def test_a_move_without_a_preceding_press_does_not_drag(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", True)
    started = _record_moves(monkeypatch)
    widget = QMainWindow()
    qtbot.addWidget(widget)

    assert SystemMoveGesture().move(widget, _mouse(QMouseEvent.Type.MouseMove)) is False
    assert started == []


def test_release_disarms_a_pending_drag(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", True)
    started = _record_moves(monkeypatch)
    widget = QMainWindow()
    qtbot.addWidget(widget)
    gesture = SystemMoveGesture()

    gesture.press(widget, _mouse(QMouseEvent.Type.MouseButtonPress))
    gesture.release()

    assert gesture.move(widget, _mouse(QMouseEvent.Type.MouseMove)) is False
    assert started == []


def test_a_press_alone_never_triggers_a_client_side_restore(monkeypatch) -> None:
    """A press is not yet a drag, and the client-side restore is destructive.

    It rewrites geometry and window state, so firing it from the press alone
    means a plain click un-maximizes the window and the maximized state that
    ``mouseDoubleClickEvent`` toggles is gone before the second click lands.
    """
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_GEOMETRY_CLEARS_MAXIMIZED", False)
    window = _StubWindow(maximized=True)
    gesture = SystemMoveGesture()

    assert gesture.press(window, _mouse(QMouseEvent.Type.MouseButtonPress)) is False
    assert window.calls == [], "the press must leave the window untouched"

    gesture.move(window, _dragging())
    assert "showNormal" in window.calls, "the drag itself still restores"


def test_a_click_that_never_becomes_a_drag_leaves_the_window_maximized(monkeypatch) -> None:
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    window = _StubWindow(maximized=True)
    gesture = SystemMoveGesture()

    gesture.press(window, _mouse(QMouseEvent.Type.MouseButtonPress))
    gesture.release()

    assert window.calls == [], "double-click-to-zoom depends on this state surviving"


def test_a_delegating_platform_still_hands_over_on_press(monkeypatch) -> None:
    # Linux is verified working this way: the compositor applies its own drag
    # threshold, so waiting for a move here would only delay handing over.
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", True)
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    started = _record_moves(monkeypatch)
    window = _StubWindow(maximized=True)

    assert SystemMoveGesture().press(window, _mouse(QMouseEvent.Type.MouseButtonPress)) is True
    assert started == [window]


def test_only_the_left_button_starts_a_drag(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    started = _record_moves(monkeypatch)
    widget = QMainWindow()
    qtbot.addWidget(widget)

    event = _mouse(QMouseEvent.Type.MouseButtonPress, button=Qt.MouseButton.RightButton)

    assert SystemMoveGesture().press(widget, event) is False
    assert started == []


def test_dragging_a_maximized_window_restores_it_first(qtbot, monkeypatch) -> None:
    """Where the platform will not un-maximize on drag, the restore happens here.

    Asserted against ``QWindow.windowStates()`` rather than ``isMaximized()``.
    The widget flag is not evidence: ``QWidget.setGeometry()`` clears
    ``Qt::WindowMaximized`` from the widget's own state on its own, so a test
    reading it passes even when the platform was never told to leave maximized
    and the window is still full size on screen.

    Pinned to the shape of a platform that needs ``showNormal()``, because that
    is the shape this suite runs under: ``conftest`` forces the offscreen
    plugin, where -- exactly as above -- the geometry call never reaches the
    QWindow. Cocoa answers the opposite way and cannot be reached from here, so
    it was measured against a real cocoa window instead.
    """
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_GEOMETRY_CLEARS_MAXIMIZED", False)
    window = QMainWindow()
    qtbot.addWidget(window)
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.resize(900, 600)
    window.show()
    window.showMaximized()
    qtbot.wait(20)
    assert window.isMaximized(), "precondition: the zoom button maximizes"

    window_gestures.begin_system_move(window)
    qtbot.wait(20)

    assert not (window.windowHandle().windowStates() & Qt.WindowState.WindowMaximized), (
        "the platform window, not just the widget, must have left maximized state"
    )
    assert window.size() == QSize(900, 600)


def test_a_compositor_that_restores_on_drag_is_left_to_do_it(qtbot, monkeypatch) -> None:
    """Mutter un-maximizes a dragged window itself; touching it here fights that.

    The compositor still owns the geometry of a window it considers maximized,
    so a client-side restore is undone, its move loop stays anchored on the
    frame the client has already left, and ``normalGeometry()`` is overwritten
    with the snapped-back full-screen rect -- losing the remembered size.
    """
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", True)
    started: list = []
    monkeypatch.setattr(
        window_gestures,
        "_request_system_move",
        lambda window: (started.append(window), True)[1],
    )
    window = QMainWindow()
    qtbot.addWidget(window)
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.resize(900, 600)
    window.show()
    window.showMaximized()
    qtbot.wait(20)
    remembered = window.normalGeometry()

    assert window_gestures.begin_system_move(window) is True
    qtbot.wait(20)

    assert started == [window], "the move must still be handed to the compositor"
    assert window.isMaximized(), "the window must be handed over as it is"
    assert window.normalGeometry() == remembered, "the remembered size must survive"


def test_the_compositor_owns_the_restore_on_linux_only() -> None:
    assert window_gestures._COMPOSITOR_RESTORES_ON_DRAG is sys.platform.startswith("linux")


def test_the_cocoa_workarounds_are_confined_to_macos() -> None:
    """Both were measured against AppKit specifically, and only apply there.

    Widening either to a platform whose ``showNormal()`` is the thing that
    clears the state, or whose move loop reads the live pointer rather than the
    event, breaks dragging there.

    Re-imported under each platform rather than compared against
    ``sys.platform``: this suite's own machine is usually the macOS one, where
    such a comparison reads ``True is True`` and passes just as happily for a
    trait that was widened to every platform.
    """
    real = sys.platform
    try:
        for platform, expected in (("darwin", True), ("linux", False), ("win32", False)):
            sys.platform = platform
            reloaded = importlib.reload(window_gestures)
            assert reloaded._GEOMETRY_CLEARS_MAXIMIZED is expected, platform
            assert reloaded._MOVE_ANCHORS_ON_ITS_EVENT is expected, platform
    finally:
        sys.platform = real
        importlib.reload(window_gestures)


class _StubWindow:
    """Minimal stand-in that records whether the platform handle was reached."""

    def __init__(self, *, full_screen: bool = False, maximized: bool = False) -> None:
        self._full_screen = full_screen
        self._maximized = maximized
        self.handle_lookups = 0
        self.calls: list[str] = []

    def isFullScreen(self) -> bool:  # noqa: N802 - Qt API shape.
        return self._full_screen

    def isMaximized(self) -> bool:  # noqa: N802 - Qt API shape.
        return self._maximized

    def windowHandle(self):  # noqa: N802 - Qt API shape.
        self.handle_lookups += 1
        return None

    def window(self) -> "_StubWindow":
        return self

    # Surfaces used by the restore path, so a test can see whether it ran.
    # Either one leaves the maximized state on the platform that uses it, and
    # the deferred hand-over depends on that having happened.
    def showNormal(self) -> None:  # noqa: N802 - Qt API shape.
        self.calls.append("showNormal")
        self._maximized = False

    def geometry(self) -> QRect:
        return QRect(100, 100, 900, 600)

    def normalGeometry(self) -> QRect:  # noqa: N802 - Qt API shape.
        return QRect(100, 100, 900, 600)

    def setGeometry(self, *args: int) -> None:  # noqa: N802 - Qt API shape.
        self.calls.append("setGeometry")
        self._maximized = False


def test_only_a_maximized_window_is_restored_before_the_drag(monkeypatch) -> None:
    # The anchor arithmetic degenerates to identity when the window is already
    # its normal size, so geometry alone cannot show whether the restore ran.
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_GEOMETRY_CLEARS_MAXIMIZED", False)
    ordinary = _StubWindow()
    window_gestures.begin_system_move(ordinary)
    assert ordinary.calls == []

    maximized = _StubWindow(maximized=True)
    window_gestures.begin_system_move(maximized)
    assert "showNormal" in maximized.calls


def test_a_full_screen_window_is_never_handed_to_the_compositor() -> None:
    # Dragging out of full screen is not a gesture any platform offers, so the
    # move must be refused before the platform handle is touched at all.
    full_screen = _StubWindow(full_screen=True)
    assert window_gestures.begin_system_move(full_screen) is False
    assert full_screen.handle_lookups == 0

    ordinary = _StubWindow()
    window_gestures.begin_system_move(ordinary)
    assert ordinary.handle_lookups == 1


def test_a_normal_window_is_not_disturbed_before_the_drag(qtbot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.wait(20)
    before = window.geometry()

    window_gestures.begin_system_move(window)
    qtbot.wait(20)

    assert window.geometry() == before


def test_restoring_keeps_the_grab_point_under_the_pointer(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    window = QMainWindow()
    qtbot.addWidget(window)
    # Frameless, as every JoyRead window is: a decorated window's deferred
    # showNormal() reconciles the restored rect by its frame margin, which
    # would blur the arithmetic being pinned here.
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.resize(900, 600)
    window.show()
    window.showMaximized()
    qtbot.wait(20)
    maximized = window.geometry()

    # Grab three-quarters of the way across the maximized title bar.
    grab = QPoint(maximized.x() + (maximized.width() * 3) // 4, maximized.y() + 10)
    monkeypatch.setattr(window_gestures, "QCursor", SimpleNamespace(pos=lambda: grab))

    window_gestures.begin_system_move(window)
    qtbot.wait(20)

    across = (grab.x() - maximized.x()) / maximized.width()
    assert window.geometry().x() == round(grab.x() - across * 900), (
        "the restored window must stay under the pointer, not jump out from under it"
    )


def test_the_maximized_state_is_cleared_before_the_window_is_placed(monkeypatch) -> None:
    """The state must leave first, or it never leaves at all.

    ``QWidget.setGeometry()`` clears ``Qt::WindowMaximized`` from the widget's
    own state without telling the QWindow. Placing the window first therefore
    makes the following ``showNormal()`` compare equal to the state the widget
    already believes it is in, return early, and never reach the platform --
    which goes on owning the geometry of a window it still thinks is maximized.
    Only the ordering can be pinned here; the desync itself is inside Qt.
    """
    monkeypatch.setattr(
        window_gestures, "QCursor", SimpleNamespace(pos=lambda: QPoint(400, 10))
    )
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_GEOMETRY_CLEARS_MAXIMIZED", False)
    window = _StubWindow(maximized=True)

    window_gestures.begin_system_move(window)

    assert window.calls == ["showNormal", "setGeometry"]


def test_the_restore_is_one_frame_change_where_geometry_clears_the_state(monkeypatch) -> None:
    """On Cocoa the frame change alone leaves maximized, and showNormal() hurts.

    There ``showNormal()`` is ``-[NSWindow zoom:]``, which animates the un-zoom
    inside a nested run loop: measured at 350.6ms against 0.9ms for the
    geometry call alone, blocking a mouse handler for a third of a second and
    painting an intermediate frame the user sees as a flicker and a bounce.
    Setting ``NSWindowAnimationBehaviorNone`` does not shorten it, so the only
    way not to pay for it is not to call it.
    """
    monkeypatch.setattr(
        window_gestures, "QCursor", SimpleNamespace(pos=lambda: QPoint(400, 10))
    )
    monkeypatch.setattr(window_gestures, "_GEOMETRY_CLEARS_MAXIMIZED", True)
    window = _StubWindow(maximized=True)

    window_gestures._restore_under_cursor(window)

    assert window.calls == ["setGeometry"], "the un-zoom animation must not be paid for"


def test_a_move_loop_that_anchors_on_its_event_is_handed_a_fresh_one(monkeypatch) -> None:
    """The restore and the hand-over must not share a mouse event.

    ``-[NSWindow performWindowDragWithEvent:]`` reads its grab point from the
    event it is given, and an ``NSEvent``'s location is relative to the window
    frame as it was when the event was created. Restoring first and handing
    over from that same event anchors the drag to the frame the restore just
    discarded: measured on macOS, a maximized window pressed and held came to
    rest at the maximized frame's origin every time, whatever position the
    restore had placed it at.
    """
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_ANCHORS_ON_ITS_EVENT", True)
    monkeypatch.setattr(
        window_gestures, "QCursor", SimpleNamespace(pos=lambda: QPoint(400, 10))
    )
    started = _record_moves(monkeypatch)
    window = _StubWindow(maximized=True)
    gesture = SystemMoveGesture()

    gesture.press(window, _mouse(QMouseEvent.Type.MouseButtonPress))

    assert gesture.move(window, _dragging()) is True
    assert window.calls, "the first move must restore"
    assert started == [], "and must not hand over on the event that measured the old frame"

    assert gesture.move(window, _dragging()) is True
    assert started == [window], "the next event is measured against the restored frame"


def test_a_move_loop_that_reads_the_live_pointer_hands_over_at_once(monkeypatch) -> None:
    # Windows takes the grab point from the pointer rather than from the event,
    # so there is nothing stale to wait out and a second event would only
    # delay the drag.
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_ANCHORS_ON_ITS_EVENT", False)
    started = _record_moves(monkeypatch)
    window = _StubWindow(maximized=True)
    gesture = SystemMoveGesture()

    gesture.press(window, _mouse(QMouseEvent.Type.MouseButtonPress))

    assert gesture.move(window, _dragging()) is True
    assert started == [window], "one event is enough when nothing was measured stale"


def test_an_ordinary_window_is_never_deferred_by_the_anchor_rule(monkeypatch) -> None:
    # The deferral exists only to outlive a restore. With no restore to do,
    # dragging a normal window must still start from the press.
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_STARTS_ON_DRAG", False)
    monkeypatch.setattr(window_gestures, "_MOVE_ANCHORS_ON_ITS_EVENT", True)
    started = _record_moves(monkeypatch)
    window = _StubWindow()

    assert SystemMoveGesture().press(window, _mouse(QMouseEvent.Type.MouseButtonPress)) is True
    assert started == [window]


# --- SystemResizeBorder ----------------------------------------------------


def test_the_border_covers_every_edge_and_corner(qtbot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(500, 400)
    window.show()
    border = install_system_resize_border(window)

    edges = {grip._edges.value for grip in border.grips}
    e = Qt.Edge
    assert edges == {
        (e.TopEdge).value,
        (e.BottomEdge).value,
        (e.LeftEdge).value,
        (e.RightEdge).value,
        (e.TopEdge | e.LeftEdge).value,
        (e.TopEdge | e.RightEdge).value,
        (e.BottomEdge | e.LeftEdge).value,
        (e.BottomEdge | e.RightEdge).value,
    }
    # Every grip must actually touch the window border it names.
    for grip in border.grips:
        rect = grip.geometry()
        assert rect.width() > 0 and rect.height() > 0
        if grip._edges & e.LeftEdge:
            assert rect.left() == 0
        if grip._edges & e.TopEdge:
            assert rect.top() == 0
        if grip._edges & e.RightEdge:
            assert rect.right() == window.width() - 1
        if grip._edges & e.BottomEdge:
            assert rect.bottom() == window.height() - 1


def test_the_border_follows_the_window_when_it_is_resized(qtbot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(500, 400)
    window.show()
    border = install_system_resize_border(window)

    window.resize(820, 640)
    qtbot.wait(10)

    right = next(g for g in border.grips if g._edges.value == (Qt.Edge.RightEdge).value)
    bottom = next(g for g in border.grips if g._edges.value == (Qt.Edge.BottomEdge).value)
    assert right.geometry().right() == window.width() - 1
    assert bottom.geometry().bottom() == window.height() - 1


def test_pressing_a_grip_asks_the_compositor_for_that_edge(qtbot, monkeypatch) -> None:
    requested: list = []
    monkeypatch.setattr(
        window_gestures,
        "begin_system_resize",
        lambda widget, edges: (requested.append(edges), True)[1],
    )
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(500, 400)
    window.show()
    border = install_system_resize_border(window)

    corner = next(
        g for g in border.grips if g._edges.value == (Qt.Edge.BottomEdge | Qt.Edge.RightEdge).value
    )
    corner.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress))

    assert [edge.value for edge in requested] == [(Qt.Edge.BottomEdge | Qt.Edge.RightEdge).value]


def test_a_maximized_window_shows_no_resize_edge(qtbot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(500, 400)
    window.show()
    border = install_system_resize_border(window)
    assert all(not grip.isHidden() for grip in border.grips)

    window.showMaximized()
    qtbot.wait(10)

    # A maximized window has no resizable edge; live grips would only offer
    # resize cursors that do nothing.
    assert all(grip.isHidden() for grip in border.grips)


def test_the_border_does_not_outlive_its_window(qtbot) -> None:
    # A Python attribute pointing back at the window would form a cycle that
    # survives WA_DeleteOnClose, leaving a wrapper around a deleted window.
    window = QMainWindow()
    qtbot.addWidget(window)
    border = install_system_resize_border(window)

    assert border.parent() is window
    assert not any(
        getattr(border, name, None) is window for name in vars(border)
    ), "SystemResizeBorder must reach its window through Qt's parent link"


# --- Regression: the reader window could not be moved or resized -----------


def test_the_reader_window_keeps_its_resize_edge_when_the_header_hides(
    qtbot, tmp_path: Path
) -> None:
    """The reader's drag handle auto-hides; its resize edge must not.

    The reader used to carry no resize affordance at all -- only the main
    window had a corner ``QSizeGrip`` -- and its window drag hung off the
    header, which ``AutoHideController`` hides after a delay. Both gestures
    died with it. The border belongs to the window, so it survives.
    """
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")

    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    assert len(window._resize_border.grips) == 8

    window.shell.auto_hide.hide_inactive()
    qtbot.wait(10)

    assert window.header.isHidden(), "precondition: the drag handle hid itself"
    assert all(not grip.isHidden() for grip in window._resize_border.grips)

    window.close()
    context.close()
