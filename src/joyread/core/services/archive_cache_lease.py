"""Scoped access to the application archive extraction pool."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
import logging
from threading import RLock
from weakref import finalize

from joyread.core.diagnostics import cache_identity_kind, reader_perf_event
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache


logger = logging.getLogger(__name__)


class ArchiveCacheScope(StrEnum):
    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


class _PoolPin:
    """The pool release one lease owes, held apart from the lease itself.

    ``weakref.finalize`` must not close over the lease, or the lease could
    never be collected and the safety net would never fire. Keeping the key
    here as well is what lets ``promote`` retarget the release in place
    instead of re-registering it.
    """

    __slots__ = ("_cache", "_key", "_scope", "_lock", "_released")

    def __init__(
        self,
        cache: ArchiveExtractionCache,
        key: str,
        scope: ArchiveCacheScope,
    ) -> None:
        self._cache = cache
        self._key = key
        self._scope = scope
        self._lock = RLock()
        self._released = False

    def retarget(self, key: str, scope: ArchiveCacheScope) -> None:
        with self._lock:
            self._key = key
            self._scope = scope

    def release(self, *, abandoned: bool = False) -> None:
        """Give the pin back to the pool, at most once."""

        with self._lock:
            if self._released:
                return
            cache, key, scope = self._cache, self._key, self._scope
            try:
                if abandoned:
                    logger.warning(
                        "Archive cache lease was dropped without close()",
                        extra={
                            "event": "archive.cache_lease.abandoned",
                            "category": "cache",
                            "status": "failed",
                            "identity_kind": cache_identity_kind(key),
                            "scope": scope.value,
                        },
                    )
                if scope == ArchiveCacheScope.EPHEMERAL:
                    # An ephemeral identity is one whose bytes must not outlive it,
                    # which is the same thing the privacy switch asks for on an
                    # encrypted document -- so it is said the same way. The pool
                    # deletes on the *last* release, so a second live lease on this
                    # document keeps the bundle it is still reading.
                    mark = getattr(cache, "mark_session_scoped", None)
                    if callable(mark):
                        mark(key)
                    else:
                        cache.purge(key)
                release = getattr(cache, "release", None)
                if callable(release):
                    release(key)
            except BaseException:
                if not abandoned:
                    # Explicit close is a recoverable lifecycle boundary: leave
                    # the pin outstanding and let the caller retry or surface the
                    # failure instead of silently leaking it for the process.
                    raise
                # A finalizer has no caller and may run while interpreter globals
                # are unravelling, so its last-chance cleanup stays best-effort.
                logger.debug("Archive cache lease finalizer release failed", exc_info=True)
                return
            self._released = True


class ArchiveCacheLease:
    """Mutable document identity with explicit cache lifetime semantics.

    External readers start with an ephemeral identity so foreground extraction
    can write immediately. Once a content hash is available, ``promote`` moves
    the accumulated cache into a persistent content-addressed identity without
    exposing key mutation to archive sessions.
    """

    def __init__(
        self,
        cache: ArchiveExtractionCache,
        document_cache_key: str,
        scope: ArchiveCacheScope,
    ) -> None:
        key = str(document_cache_key).strip()
        if not key:
            raise ValueError("document_cache_key must not be empty")
        self._cache = cache
        self._key = key
        self._scope = ArchiveCacheScope(scope)
        self._closed = False
        self._lock = RLock()
        self._pin = _PoolPin(self._cache, self._key, self._scope)
        acquire = getattr(self._cache, "acquire", None)
        if callable(acquire):
            acquire(self._key)
        # Registered only once the pin actually exists in the pool, and after
        # ``acquire`` so a failure there releases nothing it never took.
        #
        # The pool's active count is the only thing holding a bundle back from
        # eviction and the only thing that fires the session-scoped purge, and
        # both leaked for the whole process lifetime whenever a caller dropped
        # a lease instead of closing it -- which every failed archive open did.
        self._finalizer = finalize(self, self._pin.release, abandoned=True)
        logger.debug(
            "Archive cache lease acquired",
            extra={
                "event": "archive.cache_lease.acquired",
                "category": "cache",
                "status": "started",
                "identity_kind": cache_identity_kind(self._key),
                "scope": self._scope.value,
            },
        )

    @property
    def document_cache_key(self) -> str:
        with self._lock:
            return self._key

    @property
    def scope(self) -> ArchiveCacheScope:
        with self._lock:
            return self._scope

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def get(self, entry_name: str) -> bytes | None:
        with self._lock:
            if self._closed:
                return None
            return self._cache.get(self._key, entry_name)

    def get_many(self, entry_names: tuple[str, ...]) -> dict[str, bytes]:
        with self._lock:
            if self._closed:
                return {}
            return self._cache.get_many(self._key, entry_names)

    def contains_many(self, entry_names: tuple[str, ...]) -> frozenset[str]:
        """Which entries are present, checked from metadata only."""

        with self._lock:
            if self._closed:
                return frozenset()
            return self._cache.contains_many(self._key, entry_names)

    @property
    def cache_max_bytes(self) -> int:
        """The shared pool budget, for callers deciding whether a book fits."""

        return self._cache.max_bytes

    def put(self, entry_name: str, data: bytes) -> bool:
        with self._lock:
            if self._closed:
                return False
            return bool(self._cache.put(self._key, entry_name, data))

    def put_many(self, payloads: Mapping[str, bytes]) -> bool:
        with self._lock:
            if self._closed:
                return False
            return bool(self._cache.put_many(self._key, payloads))

    def is_complete(self, page_count: int, signature: str) -> bool:
        with self._lock:
            return not self._closed and self._cache.is_complete(
                self._key,
                page_count,
                signature,
            )

    def mark_complete(self, page_count: int, signature: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            return bool(self._cache.mark_complete(self._key, page_count, signature))

    def publish_complete(
        self,
        required_entries: tuple[str, ...],
        page_count: int,
        signature: str,
    ) -> bool:
        """Publish only if every required entry is present in the cache.

        The pool checks and writes the manifest under one lock, so nothing can
        remove a page between the verification and the publish.
        """

        with self._lock:
            if self._closed:
                return False
            return bool(
                self._cache.publish_complete(
                    self._key,
                    required_entries,
                    page_count,
                    signature,
                )
            )

    def purge_unpublished(self) -> bool:
        """Reclaim a partial bundle for a document that will never finish it.

        Deliberately takes no page count or signature: whether a bundle is
        published is a property of the bundle, not of the limits this session
        happens to be using.
        """

        with self._lock:
            if self._closed:
                return False
            purge_unpublished = getattr(self._cache, "purge_unpublished", None)
            if not callable(purge_unpublished):
                return False
            return bool(purge_unpublished(self._key))

    @contextmanager
    def build_guard(self) -> Iterator[bool]:
        """Keep this document's partial cache alive through publication.

        The pool lock protects each write, while this longer-lived marker
        protects the gaps between grouped writes. It does not hold either the
        lease lock or the pool lock while the caller extracts or publishes, so
        foreground cache reads remain concurrent.
        """

        with self._lock:
            if self._closed:
                registered = False
            else:
                begin_build = getattr(self._cache, "begin_build", None)
                registered = bool(
                    begin_build(self._key) if callable(begin_build) else False
                )
        try:
            yield registered
        finally:
            if registered:
                with self._lock:
                    end_build = getattr(self._cache, "end_build", None)
                    if callable(end_build):
                        # Promotion moves the pool marker with the cache key, so
                        # release whichever identity the lease currently owns.
                        end_build(self._key)

    def promote(self, persistent_key: str) -> bool:
        target = str(persistent_key).strip()
        if not target:
            raise ValueError("persistent_key must not be empty")
        with self._lock:
            if self._closed:
                return False
            if self._scope == ArchiveCacheScope.PERSISTENT:
                return self._key == target
            if not self._cache.promote(self._key, target):
                logger.warning(
                    "Archive cache lease promotion was rejected",
                    extra={
                        "event": "archive.cache_lease.promotion_rejected",
                        "category": "cache",
                        "status": "rejected",
                        "identity_kind": cache_identity_kind(self._key),
                    },
                )
                return False
            previous_kind = cache_identity_kind(self._key)
            self._key = target
            self._scope = ArchiveCacheScope.PERSISTENT
            # The pool moved the pin with the bundle, so the release this lease
            # still owes is now against the persistent identity.
            self._pin.retarget(self._key, self._scope)
            reader_perf_event(
                "archive.lease.promoted",
                previous_identity_kind=previous_kind,
                identity_kind=cache_identity_kind(target),
            )
            logger.info(
                "Archive cache lease promoted",
                extra={
                    "event": "archive.cache_lease.promoted",
                    "category": "cache",
                    "status": "finished",
                    "previous_identity_kind": previous_kind,
                    "identity_kind": cache_identity_kind(target),
                    "scope": self._scope.value,
                },
            )
            return True

    def __enter__(self) -> "ArchiveCacheLease":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._pin.release()
            # Commit the closed state only after the pool accepted the release.
            # A failed explicit close remains retryable and keeps its finalizer.
            self._closed = True
            self._finalizer.detach()
            reader_perf_event(
                "archive.lease.closed",
                scope=self._scope.value,
                identity_kind=cache_identity_kind(self._key),
            )
            logger.debug(
                "Archive cache lease closed",
                extra={
                    "event": "archive.cache_lease.closed",
                    "category": "cache",
                    "status": "finished",
                    "identity_kind": cache_identity_kind(self._key),
                    "scope": self._scope.value,
                },
            )
