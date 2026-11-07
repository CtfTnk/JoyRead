"""Carry Hidden Space events from whatever thread raised them to the GUI thread.

Two Qt-free sources report on their own threads:

* the process-wide ``VaultKeyring`` calls its lock listeners on the thread
  that locked it -- the GUI thread for Show Collections, a closing Library,
  or a storage change, but not always;
* a Library's ``SealingService`` reports the books whose job changed from its
  worker thread, several times per book as each one is sealed.

The windows react by closing hidden Readers and reloading the shelf, which is
widget work, so both arrive here as queued Qt signals. Queued even when the
lock happens on the GUI thread: the lock handler closes windows and re-renders
settings, and doing that from inside ``VaultKeyring.lock`` -- in the middle of
whatever command asked for the lock -- would re-enter that command's caller.

Sealing reports are coalesced for a moment, because one hide of fifty books
would otherwise reload the shelf a hundred and fifty times.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtCore import Signal as QtSignal

from joyread.core.services.sealing_service import SealingService
from joyread.core.vault import VaultKeyring


logger = logging.getLogger(__name__)

#: Long enough to gather one job's queued -> written -> committed -> done
#: reports into one reload, short enough to read as immediate.
_COALESCE_MS = 120


class HiddenSpaceBridge(QObject):
    """Turn keyring locks and sealing progress into GUI-thread signals."""

    #: The vault locked: decrypted material must leave the screen.
    locked = QtSignal()
    #: Hidden Space work changed these books (a tuple of book ids).
    books_changed = QtSignal(object)

    # Internal hops from foreign threads. Connected with QueuedConnection so
    # the slots always run later, on this object's (the GUI) thread.
    _lock_relay = QtSignal()
    _books_relay = QtSignal(object)

    def __init__(self, keyring: VaultKeyring, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock_relay.connect(self.locked.emit, Qt.ConnectionType.QueuedConnection)
        self._books_relay.connect(self._collect_books, Qt.ConnectionType.QueuedConnection)
        self._pending_books: set[str] = set()
        # Bumped whenever the followed SealingService changes. A report
        # already queued from the previous Library carries the old value and
        # is dropped, so a storage change never reloads with stale ids.
        self._sealing_generation = 0
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(_COALESCE_MS)
        self._flush_timer.timeout.connect(self._flush_books)
        self._remove_lock_listener: Callable[[], None] | None = keyring.add_lock_listener(self._on_locked)
        self._remove_sealing_listener: Callable[[], None] | None = None

    def attach_sealing(self, sealing: SealingService | None) -> None:
        """Follow *sealing*, the open Library's worker, instead of any earlier one."""

        self.detach_sealing()
        if sealing is not None:
            generation = self._sealing_generation

            def on_books_changed(book_ids: tuple[str, ...]) -> None:
                # The sealing worker's thread (or a caller's, for hide and unhide).
                self._books_relay.emit((generation, tuple(book_ids)))

            self._remove_sealing_listener = sealing.add_listener(on_books_changed)

    def detach_sealing(self) -> None:
        remove, self._remove_sealing_listener = self._remove_sealing_listener, None
        if remove is not None:
            remove()
        self._sealing_generation += 1
        self._pending_books.clear()
        self._flush_timer.stop()

    def close(self) -> None:
        self.detach_sealing()
        remove, self._remove_lock_listener = self._remove_lock_listener, None
        if remove is not None:
            remove()

    def _on_locked(self) -> None:
        # Any thread, outside the keyring's own lock. Emitting is all.
        self._lock_relay.emit()

    def _collect_books(self, report: tuple[int, tuple[str, ...]]) -> None:
        generation, book_ids = report
        if generation != self._sealing_generation:
            return  # From a Library this bridge no longer follows.
        self._pending_books.update(book_ids)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_books(self) -> None:
        book_ids, self._pending_books = tuple(sorted(self._pending_books)), set()
        if book_ids:
            self.books_changed.emit(book_ids)


__all__ = ["HiddenSpaceBridge"]
