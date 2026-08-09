"""Scoped access to the application archive extraction pool."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from threading import RLock

from joyread.core.diagnostics import cache_identity_kind, reader_perf_event
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache


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

    def put(self, entry_name: str, data: bytes) -> None:
        with self._lock:
            if not self._closed:
                self._cache.put(self._key, entry_name, data)

    def put_many(self, payloads: Mapping[str, bytes]) -> None:
        with self._lock:
            if not self._closed:
                self._cache.put_many(self._key, payloads)

    def is_complete(self, page_count: int, signature: str) -> bool:
        with self._lock:
            return not self._closed and self._cache.is_complete(
                self._key,
                page_count,
                signature,
            )

    def mark_complete(self, page_count: int, signature: str) -> None:
        with self._lock:
            if not self._closed:
                self._cache.mark_complete(self._key, page_count, signature)

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
                return False
            previous_kind = cache_identity_kind(self._key)
            self._key = target
            self._scope = ArchiveCacheScope.PERSISTENT
            reader_perf_event(
                "archive.lease.promoted",
                previous_identity_kind=previous_kind,
                identity_kind=cache_identity_kind(target),
            )
            return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._scope == ArchiveCacheScope.EPHEMERAL:
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
