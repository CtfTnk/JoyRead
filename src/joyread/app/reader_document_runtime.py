"""Application-owned reader document lifecycle and cache identity policy."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock
from uuid import uuid4

from joyread.app.reader_page_pipeline import (
    PageFrameDecoder,
    PreparedReaderPage,
    ReaderDocumentSource,
    ReaderPagePayload,
    ReaderPageRequest,
    SessionReaderDocumentSource,
)
from joyread.core.archive import ArchiveOpenLimits
from joyread.core.archive.service import EXPENSIVE_ARCHIVE_EXTENSIONS
from joyread.core.reader import ReaderSessionService
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache
from joyread.core.services.hash_service import HashService


class ReaderDocumentHandle(ReaderDocumentSource):
    """Opened document plus its app-level cache and promotion lifecycle."""

    def __init__(
        self,
        source: SessionReaderDocumentSource,
        source_path: Path,
        cache_key: str,
        *,
        managed: bool,
        sensitive_archive: bool,
        hash_service: HashService | None,
    ) -> None:
        self._source = source
        self._source_path = Path(source_path)
        self._cache_key = cache_key
        self._managed = bool(managed)
        self._sensitive_archive = bool(sensitive_archive)
        self._hash_service = hash_service
        self._closed = False
        self._lock = RLock()

    @property
    def page_count(self) -> int:
        return self._source.page_count

    @property
    def contents(self) -> tuple:
        return self._source.contents

    @property
    def access_mode(self):  # noqa: ANN201 - archive enum or absent for PDF.
        return self._source.access_mode

    @property
    def requires_sequential_warmup(self) -> bool:
        return self._source.requires_sequential_warmup

    @property
    def source_path(self) -> Path:
        return self._source_path

    @property
    def cache_key(self) -> str:
        with self._lock:
            return self._cache_key

    @property
    def warmup_cache_key(self) -> str | None:
        with self._lock:
            if self._closed or self._sensitive_archive:
                return None
            if self._managed or self._cache_key.startswith("external:sha256:"):
                return self._cache_key
            return None

    @property
    def needs_external_cache_promotion(self) -> bool:
        with self._lock:
            return (
                not self._closed
                and not self._managed
                and not self._sensitive_archive
                and self._hash_service is not None
                and self._source_path.suffix.lower() in EXPENSIVE_ARCHIVE_EXTENSIONS
                and not self._cache_key.startswith("external:sha256:")
            )

    def thumbnail_batch_size(self, page_index: int) -> int:
        return self._source.thumbnail_batch_size(page_index)

    def plan_read_batch(self, candidates: tuple[int, ...]) -> tuple[int, ...]:
        return self._source.plan_read_batch(candidates)

    def read_page(self, page_index: int) -> ReaderPagePayload | None:
        return self._source.read_page(page_index)

    def read_pages(self, page_indices: tuple[int, ...]) -> dict[int, ReaderPagePayload]:
        return self._source.read_pages(page_indices)

    def prepare_page(
        self,
        request: ReaderPageRequest,
        decoder: PageFrameDecoder,
    ) -> PreparedReaderPage:
        return self._source.prepare_page(request, decoder)

    def promote_external_cache(self, is_cancelled: Callable[[], bool]) -> str | None:
        """Hash a stable external source and promote its ephemeral cache."""

        with self._lock:
            if not self.needs_external_cache_promotion:
                return None
            hash_service = self._hash_service
        assert hash_service is not None
        before = _file_change_token(self._source_path)
        if before is None or is_cancelled():
            return None
        digest = hash_service.compute(self._source_path, "sha256")
        after = _file_change_token(self._source_path)
        if before != after or is_cancelled():
            return None
        persistent_key = f"external:sha256:{digest}"
        if not self._source.promote_cache(persistent_key):
            return None
        with self._lock:
            if self._closed:
                return None
            self._cache_key = persistent_key
        return persistent_key

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._source.close()


class ReaderDocumentRuntime:
    """Open Core reader sessions behind an Application-owned document port."""

    def __init__(
        self,
        session_service: ReaderSessionService,
        *,
        document_cache_key: str | None = None,
        archive_extraction_cache: ArchiveExtractionCache | None = None,
        hash_service: HashService | None = None,
    ) -> None:
        self._session_service = session_service
        self._archive_extraction_cache = archive_extraction_cache
        self._hash_service = hash_service
        self._initial_cache_key = document_cache_key or f"session:{uuid4().hex}"
        self._managed = self._initial_cache_key.startswith("file:")

    def open_document(
        self,
        source_path: str | Path,
        *,
        passwords: dict[str, str],
        skipped_archives: set[str],
        limits: ArchiveOpenLimits,
    ) -> ReaderDocumentHandle:
        path = Path(source_path)
        lease = self._new_cache_lease(path)
        try:
            session = self._open_session(
                path,
                passwords=passwords,
                skipped_archives=skipped_archives,
                limits=limits,
                lease=lease,
            )
        except Exception:
            if lease is not None:
                lease.close()
            raise
        return ReaderDocumentHandle(
            SessionReaderDocumentSource(session, self._session_service.load_pages),
            path,
            self._initial_cache_key,
            managed=self._managed,
            sensitive_archive=bool(passwords or skipped_archives),
            hash_service=self._hash_service,
        )

    def _open_session(
        self,
        path: Path,
        *,
        passwords: dict[str, str],
        skipped_archives: set[str],
        limits: ArchiveOpenLimits,
        lease: ArchiveCacheLease | None,
    ) -> object:
        try:
            return self._session_service.open_document(
                path,
                passwords=passwords,
                skipped_archives=skipped_archives,
                limits=limits,
                cache_lease=lease,
            )
        except TypeError as exc:
            unsupported = str(exc)
            if "cache_lease" not in unsupported and "limits" not in unsupported:
                raise
            if lease is not None:
                lease.close()
            if "limits" in unsupported:
                return self._session_service.open_document(
                    path,
                    passwords=passwords,
                    skipped_archives=skipped_archives,
                    nested_archive_max_depth=_legacy_depth_limit(limits.nested_archive_max_depth),
                    archive_global_file_max_depth=_legacy_depth_limit(limits.global_file_max_depth),
                )
            try:
                return self._session_service.open_document(
                    path,
                    passwords=passwords,
                    skipped_archives=skipped_archives,
                    limits=limits,
                )
            except TypeError as fallback_exc:
                if "limits" not in str(fallback_exc):
                    raise
                return self._session_service.open_document(
                    path,
                    passwords=passwords,
                    skipped_archives=skipped_archives,
                    nested_archive_max_depth=_legacy_depth_limit(limits.nested_archive_max_depth),
                    archive_global_file_max_depth=_legacy_depth_limit(limits.global_file_max_depth),
                )

    def _new_cache_lease(self, source_path: Path) -> ArchiveCacheLease | None:
        if (
            self._archive_extraction_cache is None
            or source_path.suffix.lower() not in EXPENSIVE_ARCHIVE_EXTENSIONS
        ):
            return None
        scope = ArchiveCacheScope.PERSISTENT if self._managed else ArchiveCacheScope.EPHEMERAL
        return ArchiveCacheLease(
            self._archive_extraction_cache,
            self._initial_cache_key,
            scope,
        )


def _file_change_token(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_size), int(stat.st_mtime_ns), int(getattr(stat, "st_ino", 0))


def _legacy_depth_limit(value: int | None) -> int:
    return -1 if value is None else value
