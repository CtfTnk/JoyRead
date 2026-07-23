"""In-memory cache primitives and the app-scope cache coordinator.

The coordinator (``CacheService``) owns four shared caches:

- ``reader_page_cache`` — a single byte-budgeted LRU shared across every open
  reader window. Reader viewmodels never touch this directly; they receive a
  ``NamespacedPageCache`` adapter from :meth:`CacheService.issue_reader_namespace`
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
        backing: BoundedByteCache[tuple[str, int], bytes],
        session_id: str | None = None,
    ) -> None:
        self._backing = backing
        self._session_id = session_id or uuid4().hex

    @property
    def session_id(self) -> str:
        return self._session_id

    def get(self, page_index: int) -> bytes | None:
        return self._backing.get((self._session_id, page_index))

    def put(self, page_index: int, value: bytes) -> None:
        self._backing.put((self._session_id, page_index), value)

    def clear(self) -> int:
        session_id = self._session_id
        return self._backing.purge(lambda key: key[0] == session_id)


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
        self._pins: frozenset[ThumbnailCacheKey] = frozenset()

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
        return self._backing.set_pins(self._client_id, keys)

    def release(self) -> tuple[ThumbnailCacheKey, ...]:
        self._pins = frozenset()
        return self._backing.release_client(self._client_id)


class CacheService:
    """App-scope coordinator for shared caches.

    Reader page bytes are shared across every open reader window through a
    single byte-budgeted LRU. Each :class:`ReaderViewModel` receives a
    :class:`NamespacedPageCache` issued by :meth:`issue_reader_namespace` so
    that the shared budget is enforced globally but per-session cleanup stays
    deterministic.
    """

    _COVER_PATH_OVERHEAD_BYTES = 256

    def __init__(
        self,
        archive_extraction_pool: ArchiveExtractionCache,
        reader_page_cache_max_bytes: int,
        thumbnail_cache_max_bytes: int = 64 * 1024 * 1024,
        cover_index_max_items: int = 1024,
    ) -> None:
        self.archive_extraction_pool = archive_extraction_pool
        self.reader_page_cache: BoundedByteCache[tuple[str, int], bytes] = BoundedByteCache(
            max_bytes=reader_page_cache_max_bytes,
        )
        self.thumbnail_cache = SharedThumbnailCache(max_bytes=thumbnail_cache_max_bytes)
        # The cover index stores filesystem path strings only; the bytes live
        # on disk. Budget the in-memory mirror by an approximate per-entry
        # overhead so a runaway cover index cannot squeeze the reader cache.
        self.cover_index: BoundedByteCache[str, str] = BoundedByteCache(
            max_bytes=max(1, int(cover_index_max_items)) * self._COVER_PATH_OVERHEAD_BYTES,
            sizer=lambda value: len(value) + self._COVER_PATH_OVERHEAD_BYTES,
        )

    def issue_reader_namespace(self) -> NamespacedPageCache:
        """Mint a fresh reader-page namespace bound to the shared budget."""

        return NamespacedPageCache(self.reader_page_cache)

    def issue_thumbnail_client(self, client_id: str | None = None) -> ThumbnailCacheClient:
        return self.thumbnail_cache.issue_client(client_id)

    def purge_thumbnail_source(self, document_cache_key: str) -> int:
        """Invalidate only thumbnail variants derived from one managed file."""

        prefix = f"{document_cache_key}:"
        return self.thumbnail_cache.purge(lambda key: key.source_id.startswith(prefix))

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
