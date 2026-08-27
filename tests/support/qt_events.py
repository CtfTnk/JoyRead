"""Event-loop helpers for widget tests that drive a modal popup.

A test has no event loop of its own, which makes two things awkward that the
menu widgets depend on: deferred deletions never arrive, and a menu that fails
to close leaves ``exec()`` blocking with nothing to end it. Both belong here
rather than in each test file that opens a menu.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication


def flush_deferred_deletes() -> None:
    """Run the deletions ``deleteLater()`` queued.

    Qt holds a deferred delete back until the loop level drops below the one
    it was posted from -- which, for a test with no loop of its own, is never.
    Asking for the event type by name delivers it anyway.
    """

    QApplication.processEvents()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class MenuLoopWatchdog:
    """End a menu's event loop if it outlives what the test expects.

    A menu that is destroyed, or never closed, leaves ``exec()`` waiting; the
    loop object outlives the widget, so it can still be quit from outside.
    Without this a regression hangs the whole run instead of failing one test.

    Call :meth:`watch` from inside the loop (a timer callback, say) to hand
    over the loop, and use the watchdog as a context manager around the call
    that opens the menu. ``fired`` says whether it had to step in.
    """

    def __init__(self, timeout_ms: int = 250) -> None:
        self._timeout_ms = timeout_ms
        self._loops: list[QEventLoop] = []
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._give_up)
        self.fired = False

    def watch(self, loop: QEventLoop | None) -> None:
        if loop is not None:
            self._loops.append(loop)

    def __enter__(self) -> MenuLoopWatchdog:
        self._timer.start(self._timeout_ms)
        return self

    def __exit__(self, *_exc: object) -> bool:
        self._timer.stop()
        return False

    def _give_up(self) -> None:
        self.fired = True
        for loop in self._loops:
            loop.quit()
