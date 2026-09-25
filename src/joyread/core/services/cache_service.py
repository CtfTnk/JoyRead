"""In-memory cache primitives and the P2 runtime cache boundaries.

``ReaderCacheService`` owns Reader frames, topic thumbnails, and extraction
pool access. ``LibraryCacheService`` owns the cover-path index. ``CacheService``
joins them for existing Library thumbnail callers without duplicating storage:

- ``reader_page_cache`` — viewport-sized immutable frames in one byte-budgeted
  LRU shared across every open reader window. Reader viewmodels receive a
  ``NamespacedPageCache`` adapter from :meth:`ReaderCacheService.issue_reader_namespace`
  so that closing a window can purge only that session's bytes.
- ``cover_index`` — a small in-memory map of book uuid to cover file path. The
  cover bytes themselves live on disk under ``Thumbnails/covers/``; this cache
  only mirrors the latest known path per book so the shelf does not have to
  re-stat the filesystem on every redraw.
- ``archive_extraction_pool`` — disk-backed pool for slow archive formats
  (see :mod:`joyread.core.services.archive_extraction_pool`).
- ``thumbnail_cache`` — rendered thumbnail PNGs shared by detail, reader topic,
  and cover-editor streams. Active viewport clients pin their visible window;
  only unpinned LRU entries are eligible for eviction.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Generic, TypeVar
from uuid import uuid4
from weakref import finalize

from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache


K = TypeVar("K")
V = TypeVar("V")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailCacheKey:
    """Stable identity for one rendered thumbnail variant."""

    source_id: str
    page_index: int
    width: int
    height: int


@dataclass(frozen=True)
class ThumbnailSourceIdentity:
    """Canonical cache identity shared by every thumbnail presentation."""

    document_key: str
    limits_signature: str
    security_scope: str = "shared"

    @property
    def cache_id(self) -> str:
        document_key = self.document_key.strip()
        limits = self.limits_signature.strip()
        scope = self.security_scope.strip() or "shared"
        if not document_key:
            raise ValueError("document_key must not be empty")
        return f"{document_key}:limits={limits}:scope={scope}"


def _default_sizer(value: object) -> int:
    """Best-effort byte-size estimate for cache values.

    Used by :class:`BoundedByteCache` when a caller does not supply its own
    sizer. The 64-byte fallback is intentional: opaque small payloads (file
    path strings, small dataclasses) carry no meaningful "size" for an
    LRU budget, so any constant works as long as it is consistent across
    the cache. The actual bytes those values reference live elsewhere
    (e.g., on disk for cover paths).
    """

    try:
        return len(value)  # type: ignore[arg-type]
    except TypeError:
        # Opaque payload: fall back to the agreed constant. Logged at DEBUG so
        # an unexpected non-sized value type does not silently distort the
        # budget without leaving a breadcrumb.
        logger.debug("Cache sizer fell back to constant for %r", type(value).__name__)
        return 64


class BoundedByteCache(Generic[K, V]):
    """Thread-safe LRU cache bounded by an estimated byte budget.

    Eviction is driven by ``max_bytes`` rather than item count. Values smaller
    than the budget always fit; values larger than the budget are still
    accepted (callers usually want the most recent page even if it is huge)
    but every other entry will be evicted to make room.
    """

    def __init__(self, max_bytes: int, sizer: Callable[[V], int] | None = None) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._max_bytes = int(max_bytes)
        self._sizer = sizer or _default_sizer
        self._items: "OrderedDict[K, tuple[V, int]]" = OrderedDict()
        self._current_bytes = 0
        self._lock = RLock()

    @property
    def max_bytes(self) -> int:
        with self._lock:
            return self._max_bytes

    @property
    def current_bytes(self) -> int:
        with self._lock:
            return self._current_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._items

    def get(self, key: K) -> V | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            self._items.move_to_end(key)
            value, _size = entry
            return value

    def put(self, key: K, value: V) -> None:
        size = max(0, int(self._sizer(value)))
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._current_bytes -= previous[1]
            self._items[key] = (value, size)
            self._current_bytes += size
            self._enforce_budget_locked(protect_key=key)

    def resize(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        with self._lock:
            self._max_bytes = int(max_bytes)
            self._enforce_budget_locked()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._current_bytes = 0

    def purge(self, predicate: Callable[[K], bool]) -> int:
        """Drop every entry whose key satisfies ``predicate``.

        Used by :class:`NamespacedPageCache` to free a closed reader's bytes
        without disturbing other sessions sharing the same backing cache.
        Returns the number of entries removed for instrumentation/tests.
        """

        removed = 0
        with self._lock:
            for key in list(self._items.keys()):
                if predicate(key):
                    _value, size = self._items.pop(key)
                    self._current_bytes -= size
                    removed += 1
        return removed

    def _enforce_budget_locked(self, *, protect_key: K | None = None) -> None:
        """Evict oldest entries until ``current_bytes <= max_bytes``.

        ``protect_key`` shields a freshly-inserted value from being evicted on
        its own ``put`` even when it is larger than the entire budget — the
        caller asked for it, so we keep it and dump everything else. The next
        ``put`` will evict the oversized value normally.
        """

        while self._current_bytes > self._max_bytes and self._items:
            oldest_key, (_value, size) = next(iter(self._items.items()))
            if oldest_key == protect_key and len(self._items) == 1:
                return
            self._items.popitem(last=False)
            self._current_bytes -= size


class NamespacedPageCache:
    """Per-session view onto the shared reader page cache.

    Each open reader window holds one of these. ``page_index`` keys are
    namespaced internally by a UUID, so two readers cannot collide and closing
    one window purges only that session's bytes.
    """

    def __init__(
        self,
        backing: BoundedByteCache[tuple, object],
        session_id: str | None = None,
    ) -> None:
        self._backing = backing
        self._session_id = session_id or uuid4().hex
        self._buckets: dict[int, set[tuple[int, int]]] = {}
        self._lock = RLock()

    @property
    def session_id(self) -> str:
        return self._session_id

    def get(self, page_index: int, width: int = 0, height: int = 0):  # noqa: ANN201
        key = self._key(page_index, width, height)
        value = self._backing.get(key)
        if value is not None or width > 0 or height > 0:
            return value
        with self._lock:
            buckets = sorted(
                self._buckets.get(page_index, ()),
                key=lambda size: size[0] * size[1],
                reverse=True,
            )
        found = None
        stale: list[tuple[int, int]] = []
        for bucket_width, bucket_height in buckets:
            found = self._backing.get(self._key(page_index, bucket_width, bucket_height))
            if found is not None:
                break
            stale.append((bucket_width, bucket_height))
        if stale:
            # The backing cache evicts without telling us, so a bucket that no
            # longer resolves is bookkeeping for bytes that are already gone.
            # Dropping the ones this lookup walked stops the map growing for
            # the life of the session, at no cost beyond the work already done
            # -- buckets past the first hit are simply left for a later miss.
            with self._lock:
                sizes = self._buckets.get(page_index)
                if sizes is not None:
                    sizes.difference_update(stale)
                    if not sizes:
                        self._buckets.pop(page_index, None)
        return found

    def put(self, page_index: int, value: object, width: int = 0, height: int = 0) -> None:
        with self._lock:
            self._buckets.setdefault(page_index, set()).add((width, height))
        self._backing.put(self._key(page_index, width, height), value)

    def get_suitable(self, page_index: int, target_width: int, target_height: int):  # noqa: ANN201
        with self._lock:
            buckets = tuple(self._buckets.get(page_index, ()))
        suitable = sorted(
            (
                (width, height)
                for width, height in buckets
                if width * 1.2 >= target_width and height * 1.2 >= target_height
            ),
            key=lambda size: size[0] * size[1],
        )
        for width, height in suitable:
            value = self.get(page_index, width, height)
            if value is not None:
                return value
        return None

    def clear(self) -> int:
        session_id = self._session_id
        with self._lock:
            self._buckets.clear()
        return self._backing.purge(lambda key: key[0] == session_id)

    def _key(self, page_index: int, width: int, height: int) -> tuple:
        if width <= 0 or height <= 0:
            return (self._session_id, page_index)
        return (self._session_id, page_index, int(width), int(height))


class SharedThumbnailCache:
    """App-wide byte LRU with viewport pins owned by independent clients.

    Pins are leases, not permanent cache entries. If the active viewports alone
    exceed the configured budget, the cache temporarily stays over budget
    rather than flickering visible thumbnails. Releasing a client immediately
    resumes normal LRU enforcement.
    """

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._max_bytes = int(max_bytes)
        self._current_bytes = 0
        self._items: "OrderedDict[ThumbnailCacheKey, tuple[bytes, int]]" = OrderedDict()
        self._pins_by_client: dict[str, frozenset[ThumbnailCacheKey]] = {}
        self._lock = RLock()

    @property
    def max_bytes(self) -> int:
        with self._lock:
            return self._max_bytes

    @property
    def current_bytes(self) -> int:
        with self._lock:
            return self._current_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def issue_client(self, client_id: str | None = None) -> "ThumbnailCacheClient":
        return ThumbnailCacheClient(self, client_id or uuid4().hex)

    def get(self, key: ThumbnailCacheKey) -> bytes | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            self._items.move_to_end(key)
            return entry[0]

    def put(self, key: ThumbnailCacheKey, value: bytes) -> tuple[ThumbnailCacheKey, ...]:
        size = len(value)
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._current_bytes -= previous[1]
            self._items[key] = (value, size)
            self._current_bytes += size
            return self._enforce_budget_locked()

    def set_pins(
        self,
        client_id: str,
        keys: frozenset[ThumbnailCacheKey],
    ) -> tuple[ThumbnailCacheKey, ...]:
        with self._lock:
            if keys:
                self._pins_by_client[client_id] = keys
                for key in keys:
                    if key in self._items:
                        self._items.move_to_end(key)
            else:
                self._pins_by_client.pop(client_id, None)
            return self._enforce_budget_locked()

    def release_client(self, client_id: str) -> tuple[ThumbnailCacheKey, ...]:
        return self.set_pins(client_id, frozenset())

    def resize(self, max_bytes: int) -> tuple[ThumbnailCacheKey, ...]:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        with self._lock:
            self._max_bytes = int(max_bytes)
            return self._enforce_budget_locked()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            # Pins describe current viewport interest, including entries that
            # have not been rendered yet. Live clients keep that interest across
            # invalidation so repopulated visible thumbnails stay protected.
            self._current_bytes = 0

    def purge(self, predicate: Callable[[ThumbnailCacheKey], bool]) -> int:
        """Drop cached rendered thumbnails selected by a stable source key."""

        removed = 0
        with self._lock:
            for key in tuple(self._items):
                if not predicate(key):
                    continue
                _value, size = self._items.pop(key)
                self._current_bytes -= size
                removed += 1
            for client_id, pinned in tuple(self._pins_by_client.items()):
                retained = frozenset(key for key in pinned if not predicate(key))
                if retained:
                    self._pins_by_client[client_id] = retained
                else:
                    self._pins_by_client.pop(client_id, None)
        return removed

    def promote_source(self, source_id: str, target_id: str) -> tuple[ThumbnailCacheKey, ...]:
        """Move rendered variants to a stronger document identity.

        Existing target entries win because they may have been rendered by a
        newer session. Viewport pins are migrated atomically with the payloads.
        """

        source = str(source_id).strip()
        target = str(target_id).strip()
        if not source or not target or source == target:
            return ()
        with self._lock:
            for key in tuple(self._items):
                if key.source_id != source:
                    continue
                value, size = self._items.pop(key)
                self._current_bytes -= size
                promoted = ThumbnailCacheKey(target, key.page_index, key.width, key.height)
                if promoted in self._items:
                    self._items.move_to_end(promoted)
                    continue
                self._items[promoted] = (value, size)
                self._current_bytes += size
            for client_id, pinned in tuple(self._pins_by_client.items()):
                migrated = frozenset(
                    ThumbnailCacheKey(target, key.page_index, key.width, key.height)
                    if key.source_id == source
                    else key
                    for key in pinned
                )
                self._pins_by_client[client_id] = migrated
            return self._enforce_budget_locked()

    def _enforce_budget_locked(self) -> tuple[ThumbnailCacheKey, ...]:
        pinned = set().union(*self._pins_by_client.values()) if self._pins_by_client else set()
        evicted: list[ThumbnailCacheKey] = []
        while self._current_bytes > self._max_bytes and self._items:
            victim = next((key for key in self._items if key not in pinned), None)
            if victim is None:
                break
            _value, size = self._items.pop(victim)
            self._current_bytes -= size
            evicted.append(victim)
        return tuple(evicted)


class ThumbnailCacheClient:
    """Client lease over :class:`SharedThumbnailCache`."""

    def __init__(self, backing: SharedThumbnailCache, client_id: str) -> None:
        self._backing = backing
        self._client_id = client_id
        # Display names can be reused when a controller is rebuilt while old
        # task callbacks still retain its predecessor. Pins belong to instances.
        self._pin_owner_id = uuid4().hex
        self._pins: frozenset[ThumbnailCacheKey] = frozenset()
        # Keep the callback independent from this client so the finalizer does
        # not accidentally retain the object whose abandoned pins it releases.
        self._finalizer = finalize(self, backing.release_client, self._pin_owner_id)

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def pins(self) -> frozenset[ThumbnailCacheKey]:
        return self._pins

    def get(self, key: ThumbnailCacheKey) -> bytes | None:
        return self._backing.get(key)

    def put(self, key: ThumbnailCacheKey, value: bytes) -> tuple[ThumbnailCacheKey, ...]:
        return self._backing.put(key, value)

    def set_pins(self, keys: frozenset[ThumbnailCacheKey]) -> tuple[ThumbnailCacheKey, ...]:
        self._pins = keys
        return self._backing.set_pins(self._pin_owner_id, keys)

    def release(self) -> tuple[ThumbnailCacheKey, ...]:
        evicted = self._backing.release_client(self._pin_owner_id)
        self._pins = frozenset()
        # Keep the idempotent finalizer armed. Existing clients are reusable
        # after release(), and a later set_pins() must still be returned if the
        # reused client is subsequently abandoned.
        return evicted

    def promote_source(self, source_id: str, target_id: str) -> tuple[ThumbnailCacheKey, ...]:
        self._pins = frozenset(
            ThumbnailCacheKey(target_id, key.page_index, key.width, key.height)
            if key.source_id == source_id
            else key
            for key in self._pins
        )
        return self._backing.promote_source(source_id, target_id)


class ReaderCacheService:
    """Reader frames, topic thumbnails, and extraction pool.

    This can be constructed without a Library index or database. Reader page
    namespaces share one budget and remain independently purgeable.
    """

    def __init__(
        self,
        archive_extraction_pool: ArchiveExtractionCache,
        reader_page_cache_max_bytes: int,
        thumbnail_cache_max_bytes: int = 64 * 1024 * 1024,
        reader_frame_sizer: Callable[[object], int] | None = None,
        *,
        page_cache: BoundedByteCache[tuple, object] | None = None,
        thumbnail_cache: SharedThumbnailCache | None = None,
    ) -> None:
        self.archive_extraction_pool = archive_extraction_pool
        self.reader_page_cache = (
            page_cache
            if page_cache is not None
            else BoundedByteCache(
                max_bytes=reader_page_cache_max_bytes,
                sizer=reader_frame_sizer,
            )
        )
        self.thumbnail_cache = (
            thumbnail_cache
            if thumbnail_cache is not None
            else SharedThumbnailCache(max_bytes=thumbnail_cache_max_bytes)
        )

    def issue_reader_namespace(self) -> NamespacedPageCache:
        return NamespacedPageCache(self.reader_page_cache)

    def issue_thumbnail_client(self, client_id: str | None = None) -> ThumbnailCacheClient:
        return self.thumbnail_cache.issue_client(client_id)

    def purge_thumbnail_source(self, document_cache_key: str) -> int:
        prefix = f"{document_cache_key}:"
        return self.thumbnail_cache.purge(lambda key: key.source_id.startswith(prefix))

    def with_archive_pool(self, pool: ArchiveExtractionCache) -> ReaderCacheService:
        """Stage a new pool while retaining shared frame/thumbnail budgets."""

        return ReaderCacheService(
            pool,
            self.reader_page_cache.max_bytes,
            self.thumbnail_cache.max_bytes,
            page_cache=self.reader_page_cache,
            thumbnail_cache=self.thumbnail_cache,
        )


class LibraryCacheService:
    """Library cover-path index; the actual cover bytes remain on disk."""

    _COVER_PATH_OVERHEAD_BYTES = 256

    def __init__(self, cover_index_max_items: int = 1024) -> None:
        # The cover index stores filesystem path strings only; the bytes live
        # on disk. Budget the in-memory mirror by an approximate per-entry
        # overhead so a runaway cover index cannot squeeze the reader cache.
        self.cover_index: BoundedByteCache[str, str] = BoundedByteCache(
            max_bytes=max(1, int(cover_index_max_items)) * self._COVER_PATH_OVERHEAD_BYTES,
            sizer=lambda value: len(value) + self._COVER_PATH_OVERHEAD_BYTES,
        )


class CacheService:
    """Compatibility adapter joining caches owned by two runtimes.

    ThumbnailService still uses this facade because its detail previews share
    the Reader's in-memory thumbnail cache, while its cover index belongs to
    the Library. Callers can migrate to the typed owners without changing the
    cache identities or budgets during P2.
    """

    def __init__(
        self,
        archive_extraction_pool: ArchiveExtractionCache,
        reader_page_cache_max_bytes: int,
        thumbnail_cache_max_bytes: int = 64 * 1024 * 1024,
        cover_index_max_items: int = 1024,
        reader_frame_sizer: Callable[[object], int] | None = None,
        *,
        reader_caches: ReaderCacheService | None = None,
        library_caches: LibraryCacheService | None = None,
    ) -> None:
        self.reader_caches = reader_caches or ReaderCacheService(
            archive_extraction_pool,
            reader_page_cache_max_bytes,
            thumbnail_cache_max_bytes,
            reader_frame_sizer,
        )
        self.library_caches = library_caches or LibraryCacheService(cover_index_max_items)

    @property
    def archive_extraction_pool(self) -> ArchiveExtractionCache:
        return self.reader_caches.archive_extraction_pool

    @archive_extraction_pool.setter
    def archive_extraction_pool(self, value: ArchiveExtractionCache) -> None:
        self.reader_caches.archive_extraction_pool = value

    @property
    def reader_page_cache(self) -> BoundedByteCache[tuple, object]:
        return self.reader_caches.reader_page_cache

    @property
    def thumbnail_cache(self) -> SharedThumbnailCache:
        return self.reader_caches.thumbnail_cache

    @property
    def cover_index(self) -> BoundedByteCache[str, str]:
        return self.library_caches.cover_index

    def issue_reader_namespace(self) -> NamespacedPageCache:
        """Mint a fresh reader-page namespace bound to the shared budget."""

        return self.reader_caches.issue_reader_namespace()

    def issue_thumbnail_client(self, client_id: str | None = None) -> ThumbnailCacheClient:
        return self.reader_caches.issue_thumbnail_client(client_id)

    def purge_thumbnail_source(self, document_cache_key: str) -> int:
        """Invalidate only thumbnail variants derived from one managed file."""

        return self.reader_caches.purge_thumbnail_source(document_cache_key)

    def apply_cache_budgets(
        self,
        *,
        reader_page_cache_bytes: int | None = None,
        thumbnail_cache_bytes: int | None = None,
        archive_extraction_pool_bytes: int | None = None,
    ) -> None:
        """Live-resize the caches whose budgets are user-configurable.

        Existing reader windows continue to hit the same shared cache, so the
        new budget takes effect on the next LRU pass without restarting them.
        """

        if reader_page_cache_bytes is not None:
            logger.debug("Resizing reader_page_cache to %d bytes", reader_page_cache_bytes)
            self.reader_page_cache.resize(reader_page_cache_bytes)
        if thumbnail_cache_bytes is not None:
            logger.debug("Resizing thumbnail_cache to %d bytes", thumbnail_cache_bytes)
            self.thumbnail_cache.resize(thumbnail_cache_bytes)
        if archive_extraction_pool_bytes is not None:
            logger.debug("Resizing archive_extraction_pool to %d bytes", archive_extraction_pool_bytes)
            self.archive_extraction_pool.resize(archive_extraction_pool_bytes)
