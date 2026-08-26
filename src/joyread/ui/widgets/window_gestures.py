"""Compositor-driven window move and resize for frameless windows.

Every JoyRead window is frameless, so the platform draws no title bar and no
resize border for us. The obvious replacement -- track the cursor and call
``QWidget.move()`` / ``QWidget.resize()`` -- is the wrong one. Qt can instead
ask the window manager to run the gesture, and only the window manager can:

* macOS window tiling and Windows Aero Snap trigger on a *system* drag, so a
  self-positioned window silently loses both.
* On Wayland a client may not position itself at all, and ``move()`` is simply
  ignored -- a system move is the only thing that works there.
* The compositor knows about display boundaries, scaling, and stacking; a
  hand-rolled drag rediscovers those badly or not at all.

Two entry points live here. :class:`SystemMoveGesture` is the shared press/move
bookkeeping behind dragging a window by its title bar, and
:func:`install_system_resize_border` gives a window the eight-way resize edge
the frameless hint takes away.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import QWidget

from joyread.ui.resources.styles.theme import Theme


# Windows runs its system-move loop synchronously from the button press and
# consumes the release that Qt needs to synthesise a double click, which would
# cost us double-click-to-zoom. Starting from the first *move* instead keeps
# both gestures. Elsewhere the press is the right moment: macOS in particular
# routes the drag through ``-[NSWindow performWindowDragWithEvent:]``, which
# also honours the user's "double-click a window's title bar to" preference.
_MOVE_STARTS_ON_DRAG = sys.platform == "win32"


def _restore_under_cursor(window: QWidget) -> None:
    """Un-maximize, keeping the pointer where it sits on the title bar.

    The system move APIs drag the window exactly as it is; only a title bar
    the *platform* drew un-maximizes on drag, and ours is a custom widget. So
    a maximized window would otherwise be hauled around at full size -- while
    the same window tiled by the compositor springs back to its old size,
    because there the compositor owns the restore. Doing it here makes the two
    routes agree.
    """
    cursor = QCursor.pos()
    # One coordinate space throughout -- ``geometry()`` is already in screen
    # coordinates for a top-level window, and so is ``QCursor.pos()``.
    maximized = window.geometry()
    normal = window.normalGeometry()
    window.showNormal()
    if not normal.isValid() or maximized.width() <= 0:
        return
    # Anchor the restored window so the grab point stays under the pointer
    # instead of the window jumping out from under it. This states the whole
    # rect rather than only the position: ``showNormal()`` restores geometry
    # asynchronously, and a bare ``move()`` gets partly reconciled away when
    # that lands.
    across = (cursor.x() - maximized.x()) / maximized.width()
    window.setGeometry(
        round(cursor.x() - across * normal.width()),
        maximized.y(),
        normal.width(),
        normal.height(),
    )


def begin_system_move(widget: QWidget) -> bool:
    """Ask the window manager to drag ``widget``'s window.

    A maximized window is restored first (see :func:`_restore_under_cursor`);
    a full-screen one is left alone, since dragging out of full screen is not
    a gesture any platform offers.

    Returns ``False`` when the platform declines, so callers can fall back.
    """
    window = widget.window()
    if window.isFullScreen():
        return False
    if window.isMaximized():
        _restore_under_cursor(window)
    handle = window.windowHandle()
    if handle is None:
        return False
    return bool(handle.startSystemMove())


def begin_system_resize(widget: QWidget, edges: Qt.Edge) -> bool:
    """Ask the window manager to resize ``widget``'s window from ``edges``."""
    window = widget.window()
    if window.isMaximized() or window.isFullScreen():
        return False
    handle = window.windowHandle()
    if handle is None or not edges:
        return False
    return bool(handle.startSystemResize(edges))


class SystemMoveGesture:
    """Press/move bookkeeping for handing a title-bar drag to the compositor.

    Held by a widget that acts as a drag handle. The widget forwards its mouse
    events here rather than reimplementing the platform rule above; each method
    returns ``True`` when it consumed the event.
    """

    def __init__(self) -> None:
        self._armed = False

    def press(self, widget: QWidget, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if _MOVE_STARTS_ON_DRAG:
            # Defer to the first move so Qt still sees the release.
            self._armed = True
            return False
        return begin_system_move(widget)

    def move(self, widget: QWidget, event: QMouseEvent) -> bool:
        if not self._armed or not (event.buttons() & Qt.MouseButton.LeftButton):
            return False
        self._armed = False
        return begin_system_move(widget)

    def release(self) -> None:
        self._armed = False


class _ResizeGrip(QWidget):
    """One invisible strip of a window's resize border."""

    def __init__(self, edges: Qt.Edge, cursor: Qt.CursorShape, parent: QWidget) -> None:
        super().__init__(parent)
        self._edges = edges
        self.setObjectName("WindowResizeGrip")
        self.setCursor(QCursor(cursor))
        # Nothing to paint: the grip is a hit-test region, not a decoration.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and begin_system_resize(self, self._edges):
            event.accept()
            return
        super().mousePressEvent(event)


class SystemResizeBorder(QObject):
    """Restores an eight-way resize border on a frameless window.

    The border is built from invisible child widgets pinned to the window's
    edges rather than from an event filter over the whole window. That choice
    is deliberate: a filter only sees mouse events the content did not already
    take, so any child covering the edge -- the reader's canvas covers all four
    -- would swallow the gesture. Dedicated grips sit above the content, and Qt
    gives each one its own resize cursor for free.

    It also replaces :class:`QSizeGrip`, which offers a single corner and, as a
    child of the layout, disappears whenever the widget hosting it hides.
    """

    def __init__(
        self,
        window: QWidget,
        *,
        thickness: int | None = None,
        corner: int | None = None,
    ) -> None:
        # Owned by the window, and deliberately holding no Python attribute
        # pointing back at it: ``window -> border -> window`` is a reference
        # cycle that outlives the C++ object under ``WA_DeleteOnClose``,
        # leaving a live wrapper around a deleted window. Qt's parent link
        # gets us there without one.
        super().__init__(window)
        self._thickness = Theme.window_resize_border if thickness is None else thickness
        self._corner = Theme.window_resize_corner if corner is None else corner

        edge = Qt.Edge
        cursor = Qt.CursorShape
        self._grips = tuple(
            _ResizeGrip(edges, shape, window)
            for edges, shape in (
                (edge.TopEdge, cursor.SizeVerCursor),
                (edge.BottomEdge, cursor.SizeVerCursor),
                (edge.LeftEdge, cursor.SizeHorCursor),
                (edge.RightEdge, cursor.SizeHorCursor),
                (edge.TopEdge | edge.LeftEdge, cursor.SizeFDiagCursor),
                (edge.BottomEdge | edge.RightEdge, cursor.SizeFDiagCursor),
                (edge.TopEdge | edge.RightEdge, cursor.SizeBDiagCursor),
                (edge.BottomEdge | edge.LeftEdge, cursor.SizeBDiagCursor),
            )
        )
        window.installEventFilter(self)
        self._sync()

    def _target(self) -> QWidget:
        return self.parent()

    @property
    def grips(self) -> tuple[_ResizeGrip, ...]:
        return self._grips

    def raise_border(self) -> None:
        """Lift the grips above anything stacked over them since the last sync.

        Overlays raise themselves as they appear, so a window that shows one
        must call this to keep its edge live underneath.
        """
        for grip in self._grips:
            grip.raise_()

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API.
        if watched is self._target() and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.WindowStateChange,
        ):
            self._sync()
        return super().eventFilter(watched, event)

    def _sync(self) -> None:
        # A maximized or full-screen window has no resizable edge, and leaving
        # the grips in place would show resize cursors that do nothing.
        window = self._target()
        if window.isMaximized() or window.isFullScreen():
            for grip in self._grips:
                grip.hide()
            return
        self._reposition()
        for grip in self._grips:
            grip.show()
            # Overlays are raised as they appear; stay above them so the border
            # keeps working while a transient overlay is up.
            grip.raise_()

    def _reposition(self) -> None:
        window = self._target()
        width = window.width()
        height = window.height()
        thickness = self._thickness
        corner = min(self._corner, width // 2, height // 2)
        span_x = max(0, width - 2 * corner)
        span_y = max(0, height - 2 * corner)

        top, bottom, left, right, top_left, bottom_right, top_right, bottom_left = self._grips
        top.setGeometry(corner, 0, span_x, thickness)
        bottom.setGeometry(corner, height - thickness, span_x, thickness)
        left.setGeometry(0, corner, thickness, span_y)
        right.setGeometry(width - thickness, corner, thickness, span_y)
        top_left.setGeometry(0, 0, corner, corner)
        top_right.setGeometry(width - corner, 0, corner, corner)
        bottom_left.setGeometry(0, height - corner, corner, corner)
        bottom_right.setGeometry(width - corner, height - corner, corner, corner)


def install_system_resize_border(window: QWidget, **kwargs: int) -> SystemResizeBorder:
    """Give ``window`` a compositor-driven resize border on all eight edges."""
    return SystemResizeBorder(window, **kwargs)
