"""Hidden Space covers: sealed on disk, decrypted only into memory.

A cover is a small picture of the book's first page, so leaving it as a PNG
beside a sealed book would give away exactly what sealing hides. Each hidden
book's cover is therefore its own ``.jrl`` file (content type PNG) at
``Thumbnails/covers/<book_uuid>.jrl``, sealed to the vault's public key -- so
writing one never needs the passphrase -- and decrypted on demand.

Decrypted covers are kept in a byte-bounded, in-memory LRU while the vault is
unlocked, because a shelf asks for the same covers over and over. The keyring
calls :meth:`SealedCoverStore.purge` when the vault locks, and a read that was
still in flight when that happened is not allowed to refill the cache.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
import os
from pathlib import Path
import re
import threading

from joyread.core.file_types import SEALED_EXTENSION
from joyread.core.vault import (
    ContentType,
    VaultFormatError,
    VaultKeyring,
    VaultPublicKey,
    seal_bytes,
)
from joyread.infrastructure.filesystem.path_service import PathService, WritableLocation


logger = logging.getLogger(__name__)

COVERS_DIRECTORY = "covers"
#: Enough for the shelf's working set of covers at detail size; the shelf
#: re-reads anything evicted, and a cover is cheap to decrypt.
DEFAULT_COVER_CACHE_BYTES = 32 * 1024 * 1024
#: A cover is a thumbnail. Refusing anything larger keeps one damaged or
#: planted file from making the shelf allocate a whole book's worth of memory.
MAX_COVER_BYTES = 16 * 1024 * 1024

_BOOK_UUID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_PARTIAL_SUFFIX = ".partial"


class SealedCoverStore:
    """Seal, read, and forget one Library's Hidden Space covers. Thread-safe."""

    def __init__(
        self,
        paths: PathService,
        keyring: VaultKeyring,
        *,
        cache_max_bytes: int = DEFAULT_COVER_CACHE_BYTES,
    ) -> None:
        self._paths = paths
        self._keyring = keyring
        self._cache_max_bytes = max(0, int(cache_max_bytes))
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._lock = threading.Lock()
        # Bumped by every purge, write and delete. A read compares it before
        # caching what it decrypted, so a cover decrypted just before a lock
        # -- or just before the cover was replaced -- is returned to its
        # caller but never kept.
        self._generation = 0
        self._remove_lock_listener = keyring.add_lock_listener(self.purge)

    @property
    def covers_dir(self) -> Path:
        # Resolved per call from the live storage root, like the thumbnail
        # service's covers, so the store follows a Library that moves.
        return self._paths.resolve(WritableLocation.THUMBNAILS, COVERS_DIRECTORY)

    def path_for(self, book_uuid: str) -> Path:
        if not _BOOK_UUID.fullmatch(book_uuid):
            raise ValueError("Not a book id that can name a cover file.")
        return self.covers_dir / f"{book_uuid}{SEALED_EXTENSION}"

    def is_sealed_cover(self, path: Path) -> bool:
        """Whether *path* is a file this store owns, for maintenance scans."""

        candidate = Path(path)
        return (
            candidate.suffix.lower() == SEALED_EXTENSION
            and candidate.parent == self.covers_dir
            and _BOOK_UUID.fullmatch(candidate.stem) is not None
        )

    def write(self, book_uuid: str, png: bytes, public_key: VaultPublicKey) -> Path:
        """Seal *png* as the book's cover, replacing any previous one atomically.

        Written to a partial file, flushed to disk, then renamed over the old
        cover, so a crash leaves either the old cover or the new one, never a
        torn file. Needs only the public key: covers are sealed while locked.
        """

        if len(png) > MAX_COVER_BYTES:
            raise ValueError("A cover image is larger than a cover can be.")
        target = self.path_for(book_uuid)
        sealed = seal_bytes(bytes(png), public_key, ContentType.PNG)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + _PARTIAL_SUFFIX)
        try:
            with partial.open("wb") as handle:
                handle.write(sealed)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        self._forget(book_uuid)
        return target

    def read(self, book_uuid: str) -> bytes | None:
        """The decrypted cover, or ``None`` if the book has no sealed cover.

        Raises ``VaultLockedError`` while the vault is locked, and the
        container's errors for a damaged cover or one sealed to another vault.
        """

        with self._lock:
            cached = self._cache.get(book_uuid)
            if cached is not None:
                self._cache.move_to_end(book_uuid)
                return cached
            generation = self._generation
        path = self.path_for(book_uuid)
        try:
            sealed = self._keyring.open_sealed(path)
        except FileNotFoundError:
            return None
        try:
            if sealed.content_type is not ContentType.PNG:
                raise VaultFormatError("A sealed cover does not hold an image.")
            if sealed.content_length > MAX_COVER_BYTES:
                raise VaultFormatError("A sealed cover is larger than a cover can be.")
            data = sealed.read_all()
        finally:
            sealed.close()
        self._remember(book_uuid, data, generation)
        return data

    def delete(self, book_uuid: str) -> None:
        self._forget(book_uuid)
        path = self.path_for(book_uuid)
        path.unlink(missing_ok=True)
        path.with_name(path.name + _PARTIAL_SUFFIX).unlink(missing_ok=True)

    def purge(self) -> None:
        """Drop every decrypted cover. The keyring calls this on lock."""

        with self._lock:
            self._generation += 1
            dropped = len(self._cache)
            self._cache.clear()
            self._cache_bytes = 0
        if dropped:
            logger.debug(
                "Sealed cover cache purged",
                extra={"event": "vault.covers.purged", "category": "vault", "count": dropped},
            )

    def close(self) -> None:
        self._remove_lock_listener()
        self.purge()

    @property
    def cached_bytes(self) -> int:
        with self._lock:
            return self._cache_bytes

    def _remember(self, book_uuid: str, data: bytes, generation: int) -> None:
        if len(data) > self._cache_max_bytes:
            return
        with self._lock:
            # A purge since this read began means the vault locked meanwhile.
            # The caller still gets its cover; the cache does not keep it.
            if generation != self._generation or not self._keyring.is_unlocked:
                return
            previous = self._cache.pop(book_uuid, None)
            if previous is not None:
                self._cache_bytes -= len(previous)
            self._cache[book_uuid] = data
            self._cache_bytes += len(data)
            while self._cache_bytes > self._cache_max_bytes and self._cache:
                _evicted, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= len(evicted)

    def _forget(self, book_uuid: str) -> None:
        with self._lock:
            self._generation += 1
            previous = self._cache.pop(book_uuid, None)
            if previous is not None:
                self._cache_bytes -= len(previous)


__all__ = [
    "COVERS_DIRECTORY",
    "DEFAULT_COVER_CACHE_BYTES",
    "MAX_COVER_BYTES",
    "SealedCoverStore",
]
