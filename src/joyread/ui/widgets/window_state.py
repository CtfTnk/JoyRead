"""Who is maximized, and what size it returns to.

These are the two facts the window chrome keeps getting wrong, because Qt
offers four separate answers to them and three are unreliable:

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

See ``docs/WINDOW_STATE_INVESTIGATION.md`` for the measurements.
"""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QObject, QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


# Whether the platform keeps a zoom state of its own that Qt does not reliably
# mirror. On macOS the user can maximize a window in ways Qt never initiates --
# the green button, a title-bar double click, dragging to the top edge to tile
# it -- and after any of those ``QWidget.isMaximized()`` and
# ``NSWindow.isZoomed()`` can disagree indefinitely.
_PLATFORM_OWNS_THE_ZOOM_STATE = sys.platform == "darwin"

# Whether ``normalGeometry()`` survives a platform-driven resize. On macOS it
# does not; see the module docstring. Everywhere else it is still the best
# answer available, and changing that is a behaviour change on platforms this
# was not measured against.
_PLATFORM_FORGETS_THE_RESTORE_SIZE = sys.platform == "darwin"

# Whether setting the geometry is by itself enough to leave the maximized
# state. On Cocoa it is: AppKit drops the zoomed state as soon as the frame
# stops matching the zoomed one -- measured from a tiled window, a 900x600
# setGeometry left isMaximized, QWindow.windowStates() and NSWindow.isZoomed()
# all agreeing, in 1.2ms. Avoiding ``showNormal()`` there is the point rather
# than a bonus: on Cocoa it is ``-[NSWindow zoom:]``, which animates the
# un-zoom inside a nested run loop and blocks its caller for ~350ms, and
# ``NSWindowAnimationBehaviorNone`` does not shorten it.
#
# ``zoom:`` is also the wrong answer for a different reason: measured from a
# tiled window it restores to AppKit's *own* saved frame, which the same
# animation has already overwritten -- 1492x929 rather than the 900x600 the
# user chose.
_GEOMETRY_ALONE_LEAVES_MAXIMIZED = sys.platform == "darwin"

_STORE_NAME = "JoyReadRestoreGeometry"


def _on_cocoa() -> bool:
    """Whether the Cocoa QPA backend is the one in use.

    The gate is the backend, not the OS. Under the offscreen and minimal
    plugins -- which macOS can perfectly well be running, and which the test
    suite forces -- ``winId()`` is not an NSView pointer, and handing it to
    ``objc_object()`` segfaults rather than raising, straight past any except.
    """
    return QGuiApplication.platformName() == "cocoa"


def _native_window(window: QWidget):
    """The ``NSWindow`` behind ``window``. Only safe once :func:`_on_cocoa`."""
    import objc

    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(window.winId())))
    return view.window()


def _appkit_is_zoomed(window: QWidget) -> bool | None:
    """``NSWindow.isZoomed()``, or ``None`` if AppKit cannot be reached.

    ``None`` rather than ``False``: an NSWindow we could not read is not
    evidence that the window is unmaximized, and saying so would strand a
    maximized window exactly the way the widget flag already does.
    """
    if not _on_cocoa():
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

    Deliberately not ``QWidget.isMaximized()``. That flag is cleared by
    ``setGeometry()`` without the platform being told, which is what let a
    window get stuck: our code read ``False``, stopped trying to restore it,
    and the window manager went on refusing to move or resize a window it
    still considered maximized.
    """
    if _PLATFORM_OWNS_THE_ZOOM_STATE:
        zoomed = _appkit_is_zoomed(window)
        if zoomed is not None:
            return zoomed
    return window.isMaximized()


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
    changes. That distinction is the whole point: a platform maximize
    animation is a torrent of geometry changes, and latching on those is
    exactly how ``normalGeometry()`` destroys itself. At these moments nothing
    is animating and the window is the size the user chose.

    A maximized window has nothing worth remembering, so it is ignored.
    """
    if is_maximized(window) or window.isFullScreen():
        return
    _store(window).rect = window.geometry()


def restore_geometry(window: QWidget) -> QRect:
    """The size ``window`` should return to when it un-maximizes."""
    if not _PLATFORM_FORGETS_THE_RESTORE_SIZE:
        return window.normalGeometry()
    remembered = _store(window).rect
    # Nothing latched yet -- a window maximized before it was ever dragged or
    # resized. Qt's answer is wrong only *after* a platform animation, so it is
    # still the best fallback here.
    return remembered if remembered.isValid() else window.normalGeometry()


def leave_maximized(window: QWidget, *, geometry: QRect) -> None:
    """Take ``window`` out of maximized state and place it at ``geometry``.

    The single way out of maximized state, shared by the zoom button and by a
    title-bar drag; they differ only in the rectangle they ask for.

    Where ``showNormal()`` is needed at all it must come first, and the order
    is load-bearing. ``QWidget.setGeometry()`` clears ``Qt::WindowMaximized``
    from the widget's own state without telling the QWindow, so a
    ``showNormal()`` after it compares equal to the state the widget already
    believes it is in, returns early, and the platform is never told to leave
    maximized -- leaving the window manager owning the geometry of a window it
    still thinks is maximized. Reproduced identically under the offscreen and
    minimal QPA plugins, so this is QWidget behaviour rather than one backend's
    quirk.
    """
    if not geometry.isValid():
        window.showNormal()
        return
    if not _GEOMETRY_ALONE_LEAVES_MAXIMIZED:
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
