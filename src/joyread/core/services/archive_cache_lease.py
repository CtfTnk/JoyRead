"""Scoped access to the application archive extraction pool."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
import logging
from threading import RLock

from joyread.core.diagnostics import cache_identity_kind, reader_perf_event
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache


logger = logging.getLogger(__name__)


class ArchiveCacheScope(StrEnum):
    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


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
        acquire = getattr(self._cache, "acquire", None)
        if callable(acquire):
            acquire(self._key)
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

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._scope == ArchiveCacheScope.EPHEMERAL:
                # An ephemeral identity is one whose bytes must not outlive it,
                # which is the same thing the privacy switch asks for on an
                # encrypted document -- so it is said the same way. The pool
                # deletes on the *last* release, so a second live lease on this
                # document keeps the bundle it is still reading.
                mark = getattr(self._cache, "mark_session_scoped", None)
                if callable(mark):
                    mark(self._key)
                else:
                    self._cache.purge(self._key)
            release = getattr(self._cache, "release", None)
            if callable(release):
                release(self._key)
            self._closed = True
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
