"""Carry archive-pool usage changes from worker threads to the GUI thread.

The extraction pool is Qt-free and is written from whichever worker happens to
be caching pages. The settings page needs to show that usage live, so the two
have to be connected without either learning about the other: the pool reports
a plain integer to a listener, and this bridge turns that into a queued Qt
signal, which is what makes the hop across threads safe.

Queued rather than polled on purpose. A timer would either lag behind real
writes or burn wakeups while nothing is caching.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject
from PySide6.QtCore import Signal as QtSignal

logger = logging.getLogger(__name__)


class ArchivePoolUsageBridge(QObject):
    """Turn pool usage callbacks into a GUI-thread signal."""

    #: Emitted on the GUI thread with the pool's new byte usage.
    #:
    #: ``qint64``, not ``int``: Qt maps a Python ``int`` parameter to a C++
    #: 32-bit ``int``, which overflows at 2.147 GB. The pool budget defaults to
    #: 5 GB and reaches 50 GB, so a default install crosses that on its way to
    #: being full and every write then raises out of the listener.
    usage_changed = QtSignal("qint64")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool: object | None = None

    def attach(self, pool: object) -> None:
        """Listen to ``pool``, detaching from any previously attached one.

        A storage transition replaces the pool, so the old one is released
        first -- otherwise a retired pool could keep reporting usage for a
        library that is no longer open.
        """

        self.detach()
        listener = getattr(pool, "set_usage_listener", None)
        if not callable(listener):
            # Test doubles and the null pool need not support this.
            return
        listener(self._on_usage_changed)
        self._pool = pool

    def detach(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        listener = getattr(pool, "set_usage_listener", None)
        if callable(listener):
            listener(None)

    def _on_usage_changed(self, usage: int) -> None:
        # Runs on the caching worker thread, under the pool lock. Emitting is
        # all that may happen here: the connection is queued, so the receiving
        # slot runs later on the GUI thread rather than inside the lock.
        self.usage_changed.emit(int(usage))
