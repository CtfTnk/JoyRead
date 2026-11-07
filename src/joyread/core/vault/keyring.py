"""The process-wide holder of the unlocked vault key.

Readers are process-wide: one archive service and one PDF service serve every
window. The vault belongs to one Library. So there is exactly one keyring per
process, the Library that owns the vault unlocks it, and everything that reads
a sealed book asks the keyring rather than holding a key of its own.

Locking has to reach readers that are already open. A :class:`SealedFile`
keeps its derived payload key, so dropping the vault key alone would leave
every open book readable. The keyring therefore remembers each file it opened
and revokes all of them when it locks: their next read fails with
:class:`VaultLockedError`. That makes "locked" true for the whole process,
whether or not every window remembered to close its book first.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
import threading
import weakref

from joyread.core.vault.container import SealedFile
from joyread.core.vault.errors import VaultLockedError
from joyread.core.vault.keys import VaultPrivateKey


logger = logging.getLogger(__name__)

LockListener = Callable[[], None]


class VaultKeyring:
    """Thread-safe: unlock and lock run on the UI side, opens on workers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._key: VaultPrivateKey | None = None
        # Weak, so a file closed and dropped by its session is not kept alive
        # just to be revoked later.
        self._open_files: weakref.WeakSet[SealedFile] = weakref.WeakSet()
        self._listeners: list[LockListener] = []

    @property
    def is_unlocked(self) -> bool:
        with self._lock:
            return self._key is not None

    @property
    def key_id(self) -> bytes | None:
        """The unlocked vault's key id, which names no secret."""

        with self._lock:
            return None if self._key is None else self._key.public_key.key_id

    def unlock(self, private_key: VaultPrivateKey) -> None:
        """Hold *private_key* until :meth:`lock`.

        Unlocking a different vault while one is open locks the first one,
        with everything locking implies: a book opened under the old key must
        not stay readable once another Library's vault is in use.
        """

        with self._lock:
            current = self._key
            if current is not None and current.public_key == private_key.public_key:
                return
            # Swapped in one step, so no open can slip in under the old key
            # between the old vault locking and the new one unlocking.
            files = self._take_open_files() if current is not None else []
            self._key = private_key
            listeners = tuple(self._listeners)
        if current is not None:
            self._finish_lock(files, listeners)

    def lock(self) -> int:
        """Drop the key and revoke every file opened with it.

        Returns how many open files were revoked. Lock listeners run after the
        key is gone, so a listener that tries to read finds the vault locked.
        """

        with self._lock:
            was_unlocked = self._key is not None
            self._key = None
            files = self._take_open_files()
            listeners = tuple(self._listeners)
        if was_unlocked or files:
            self._finish_lock(files, listeners)
        return len(files)

    def _take_open_files(self) -> list[SealedFile]:
        files = list(self._open_files)
        self._open_files.clear()
        return files

    @staticmethod
    def _finish_lock(files: list[SealedFile], listeners: tuple[LockListener, ...]) -> None:
        # Outside the keyring lock: a listener is foreign code, and holding
        # this lock while it runs would let it deadlock against a worker that
        # is waiting here to open a book.
        for sealed in files:
            sealed.revoke()
        for listener in listeners:
            try:
                listener()
            except Exception:
                # One broken listener must not leave the others holding
                # decrypted covers or pages after the vault locked.
                logger.error(
                    "A vault lock listener failed",
                    exc_info=True,
                    extra={"event": "vault.lock.listener_failed", "category": "vault", "status": "failed"},
                )

    def open_sealed(self, path: Path | str) -> SealedFile:
        """Open a sealed file with the unlocked key.

        Raises :class:`VaultLockedError` while locked, and the container's own
        errors for a file that is damaged or sealed to another vault.
        """

        with self._lock:
            key = self._key
        if key is None:
            raise VaultLockedError("Hidden Space is locked.")
        # The unwrap runs outside the lock: it costs about a millisecond and
        # would otherwise serialize every book open in the process.
        sealed = SealedFile.open(path, key)
        with self._lock:
            if self._key is key:
                self._open_files.add(sealed)
                return sealed
        # Locked, or switched to another vault, while the file was opening.
        sealed.revoke()
        raise VaultLockedError("Hidden Space is locked.")

    def add_lock_listener(self, listener: LockListener) -> Callable[[], None]:
        """Call *listener* after every lock. Returns a function that removes it.

        For holders of decrypted material the keyring cannot reach itself,
        such as an in-memory cover cache.
        """

        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return remove


__all__ = ["LockListener", "VaultKeyring"]
