"""Who is maximized, and what size the window returns to.

These are the two facts the window chrome kept getting wrong, because Qt offers
four separate answers to them and three are unreliable:

* ``QWidget.isMaximized()`` is cleared by ``QWidget.setGeometry()`` without the
  platform being told, so it reads ``False`` for a window the window manager
  still has maximized.
* ``QWindow.windowStates()`` is closer to the truth but is sometimes never told
  about a state change the *platform* initiated.
* ``QWidget.normalGeometry()`` is destroyed outright by an animated platform
  resize. Measured on macOS: dragging a window to the top edge animates the
  fill over ~350ms, Qt receives 19 intermediate frames, does not yet know a
  maximize is under way, and records each one as the new normal geometry. A
  900x600 window comes out the far side believing its normal size is 1512x949.
* ``NSWindow.isZoomed()`` is correct, and invisible to all of the above.

So this module owns both answers. :func:`is_maximized` asks the platform rather
than the widget, and the restore size is *latched from user intent* rather than
read back from the window -- see :func:`remember_restore_geometry`.

Every workaround here keys off the QPA backend rather than ``sys.platform``,
because every one of them is a property of that backend and not of the
operating system -- macOS running the offscreen plugin behaves the X11 way for
all of it. :func:`on_cocoa` answers that question once. The behaviour
predicates below each return it, and are kept separate from it deliberately:
:func:`on_cocoa` is also the *safety* gate on reaching into AppKit at all, and
a test that wants to exercise one of the behaviours must not be able to switch
that guard off by accident. See ``docs/WINDOW_STATE_INVESTIGATION.md`` for the
measurements.
"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QObject, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


# How far a window's size may sit from the screen's usable area and still count
# as filling it. A maximized window matches it exactly; the slack is for a
# platform that insets the frame by a pixel or two.
_FILLS_THE_SCREEN_SLACK = 4

_STORE_NAME = "JoyReadRestoreGeometry"


def on_cocoa() -> bool:
    """Whether the Cocoa QPA backend is the one in use.

    Both the source of every behaviour predicate below and, in
    :func:`_appkit_is_zoomed`, the guard against reaching into AppKit when
    there is no AppKit to reach: under the offscreen and minimal plugins
    ``winId()`` is not an NSView pointer, and handing it to ``objc_object()``
    segfaults rather than raising, straight past any ``except``.
    """
    return QGuiApplication.platformName() == "cocoa"


def _platform_forgets_the_restore_size() -> bool:
    """Whether ``normalGeometry()`` survives a platform-driven resize.

    On Cocoa it does not -- see the module docstring. Everywhere else Qt's
    answer is still the best available, and second-guessing it would be a
    behaviour change on a platform none of this was measured against.
    """
    return on_cocoa()


def _geometry_alone_leaves_maximized() -> bool:
    """Whether setting the frame is by itself enough to leave maximized.

    On Cocoa it is: measured from a tiled window, a 900x600 ``setGeometry``
    left ``isMaximized()``, ``QWindow.windowStates()`` and
    ``NSWindow.isZoomed()`` all agreeing, in 1.2ms. Skipping ``showNormal()``
    there is the point rather than a bonus -- on Cocoa it is
    ``-[NSWindow zoom:]``, which animates the un-zoom inside a nested run loop
    and blocks its caller for ~350ms, and ``NSWindowAnimationBehaviorNone``
    does not shorten it. ``zoom:`` is wrong for a second reason too: it
    restores to AppKit's own saved frame, which that same animation has
    already overwritten.
    """
    return on_cocoa()


def _native_window(window: QWidget):
    """The ``NSWindow`` behind ``window``. Only safe once :func:`on_cocoa`."""
    import objc

    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(window.winId())))
    return view.window()


def _appkit_is_zoomed(window: QWidget) -> bool | None:
    """``NSWindow.isZoomed()``, or ``None`` if AppKit cannot be reached.

    ``None`` rather than ``False``: an NSWindow we could not read is not
    evidence that the window is unmaximized, and saying so would strand a
    maximized window exactly the way the widget flag already does.
    """
    if not on_cocoa():
        return None
    # winId() creates the native window as a side effect, so only ask once the
    # window already has one.
    if window.windowHandle() is None:
        return None
    try:
        native = _native_window(window)
    except Exception:
        return None
    return None if native is None else bool(native.isZoomed())


def is_maximized(window: QWidget) -> bool:
    """Whether the *platform* considers ``window`` maximized.

    Deliberately not ``QWidget.isMaximized()``, which is used only as the
    fallback. That flag is cleared by ``setGeometry()`` without the platform
    being told, which is what let a window get stuck: our code read ``False``,
    stopped trying to restore it, and the window manager went on refusing to
    move or resize a window it still considered maximized.
    """
    zoomed = _appkit_is_zoomed(window)
    return window.isMaximized() if zoomed is None else zoomed


def fills_the_screen(window: QWidget) -> bool:
    """Whether ``window`` still occupies the whole usable screen area.

    Asked before un-maximizing, because that only means "shrink a window that
    fills the screen". A window the platform still calls maximized but which no
    longer fills it has been resized by the user since -- macOS keeps a native
    resize edge on a tiled window, which bypasses our own resize grips entirely
    and leaves the window tiled at a size the user chose. Shrinking it then
    throws that size away.

    Stated in geometry we can see rather than in what the platform means by its
    state flags, so it holds whatever the platform's tiling semantics turn out
    to be.
    """
    screen = window.screen()
    if screen is None:
        # No screen to compare against. Assume the caller's reading of the
        # window state rather than silently skipping a restore it wanted.
        return True
    available = screen.availableGeometry().size()
    current = window.geometry().size()
    return (
        abs(current.width() - available.width()) <= _FILLS_THE_SCREEN_SLACK
        and abs(current.height() - available.height()) <= _FILLS_THE_SCREEN_SLACK
    )


class _RestoreGeometry(QObject):
    """The size a window should return to, held for the window's lifetime.

    A child of the window rather than an entry in a table keyed by it: Qt's
    parent link dies with the C++ object, where a module-level mapping would
    keep a wrapper alive around a deleted window.
    """

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setObjectName(_STORE_NAME)
        self.rect = QRect()


def _store(window: QWidget) -> _RestoreGeometry:
    found = window.findChild(
        _RestoreGeometry, _STORE_NAME, Qt.FindChildOption.FindDirectChildrenOnly
    )
    return found if found is not None else _RestoreGeometry(window)


def remember_restore_geometry(window: QWidget) -> None:
    """Latch the size to come back to, from a moment of user intent.

    Called when the user is *about* to do something -- start a drag, start a
    resize, press the zoom button -- rather than from observed geometry
    changes. That distinction is the whole point: a platform maximize animation
    is a torrent of geometry changes, and latching on those is exactly how
    ``normalGeometry()`` destroys itself. At these moments nothing is animating
    and the window is the size the user chose.

    A maximized window has nothing worth remembering, so it is ignored. The
    latch is kept on every platform even though only Cocoa reads it back, so
    that the recording and the decision to trust it stay independent.
    """
    if is_maximized(window) or window.isFullScreen():
        return
    _store(window).rect = window.geometry()


def restore_geometry(window: QWidget) -> QRect:
    """The size ``window`` should return to when it un-maximizes.

    Ours where the platform forgets it -- see
    :func:`_platform_forgets_the_restore_size`.
    """
    if not _platform_forgets_the_restore_size():
        return window.normalGeometry()
    remembered = _store(window).rect
    # Nothing latched yet -- a window maximized before it was ever dragged or
    # resized. Qt's answer is wrong only *after* a platform animation, so it is
    # still the best fallback here.
    return remembered if remembered.isValid() else window.normalGeometry()


def leave_maximized(window: QWidget, *, geometry: QRect) -> None:
    """Take ``window`` out of maximized state and place it at ``geometry``.

    The single way out, shared by the zoom button and by a title-bar drag; they
    differ only in the rectangle they ask for. An invalid rectangle means the
    caller has no opinion about where the window should land, only that it must
    stop being maximized.

    Where ``showNormal()`` is needed at all it must come first, and the order is
    load-bearing. ``QWidget.setGeometry()`` clears ``Qt::WindowMaximized`` from
    the widget's own state without telling the QWindow, so a ``showNormal()``
    after it compares equal to the state the widget already believes it is in,
    returns early, and the platform is never told to leave maximized -- leaving
    the window manager owning the geometry of a window it still thinks is
    maximized. Reproduced identically under the offscreen and minimal QPA
    plugins, so this is QWidget behaviour rather than one backend's quirk.
    """
    if not geometry.isValid():
        window.showNormal()
        return
    if not _geometry_alone_leaves_maximized():
        window.showNormal()
    window.setGeometry(geometry)


def toggle_maximized(window: QWidget) -> None:
    """What a zoom button does, asking the platform rather than the widget."""
    if is_maximized(window):
        leave_maximized(window, geometry=restore_geometry(window))
        return
    # About to maximize on purpose, so this is the size to come back to.
    remember_restore_geometry(window)
    window.showMaximized()
