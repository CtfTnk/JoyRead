"""Unit tests for compositor-driven window move and resize."""

from __future__ import annotations

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
    """
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
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

    # Surface used by the restore path, so a test can see whether it ran.
    def showNormal(self) -> None:  # noqa: N802 - Qt API shape.
        self.calls.append("showNormal")

    def geometry(self) -> QRect:
        return QRect(100, 100, 900, 600)

    def normalGeometry(self) -> QRect:  # noqa: N802 - Qt API shape.
        return QRect(100, 100, 900, 600)

    def setGeometry(self, *args: int) -> None:  # noqa: N802 - Qt API shape.
        self.calls.append("setGeometry")


def test_only_a_maximized_window_is_restored_before_the_drag(monkeypatch) -> None:
    # The anchor arithmetic degenerates to identity when the window is already
    # its normal size, so geometry alone cannot show whether the restore ran.
    monkeypatch.setattr(window_gestures, "_COMPOSITOR_RESTORES_ON_DRAG", False)
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
    window = _StubWindow(maximized=True)

    window_gestures.begin_system_move(window)

    assert window.calls == ["showNormal", "setGeometry"]


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
