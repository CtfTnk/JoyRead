"""Application-owned reader document lifecycle and cache identity policy."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
from threading import RLock
from time import perf_counter
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


logger = logging.getLogger(__name__)


class ReaderDocumentHandle(ReaderDocumentSource):
    """Opened document plus its app-level cache and promotion lifecycle."""

    def __init__(
        self,
        source: SessionReaderDocumentSource,
        source_path: Path,
        cache_key: str,
        *,
        managed: bool,
        needs_extraction_pool: bool,
        hash_service: HashService | None,
    ) -> None:
        self._source = source
        self._source_path = Path(source_path)
        self._cache_key = cache_key
        self._managed = bool(managed)
        # Whether this document is expensive enough to want the pool at all --
        # by container format or by encryption. Replaces a suffix test that
        # could not see the second reason, and a `sensitive_archive` flag that
        # used to keep encrypted documents *out* of the pool entirely.
        self._needs_extraction_pool = bool(needs_extraction_pool)
        self._hash_service = hash_service
        self._document_id = uuid4().hex
        self._opened_at = perf_counter()
        self._closed = False
        self._lock = RLock()

    @property
    def page_count(self) -> int:
        return self._source.page_count

    @property
    def document_id(self) -> str:
        return self._document_id

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
        """The identity a background warmup may build a cache product under.

        Encrypted archives are included. They used to be excluded, which meant
        the format that benefits most from being pre-converted was the one
        format never pre-converted -- the whole first pass through the book
        paid full decryption cost per page turn. Warming them requires handing
        the password to the warmup, which ``ReaderViewModel`` does when it
        calls ``ArchiveWarmupCoordinator.acquire``, and puts plaintext in the
        pool; both are accepted deliberately.
        """

        with self._lock:
            if self._closed:
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
                and self._hash_service is not None
                and self._needs_extraction_pool
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

    def prepare_thumbnail_pages(
        self,
        page_indices: tuple[int, ...],
        size: tuple[int, int],
    ) -> list[PreparedReaderPage | None] | None:
        return self._source.prepare_thumbnail_pages(page_indices, size)

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
        started = perf_counter()
        logger.info(
            "Reader document close started",
            extra={
                "event": "reader.document.close.started",
                "category": "reader",
                "status": "started",
                "document_id": self._document_id,
            },
        )
        try:
            self._source.close()
        except Exception as exc:
            logger.error(
                "Reader document close failed",
                exc_info=True,
                extra={
                    "event": "reader.document.close.failed",
                    "category": "reader",
                    "status": "failed",
                    "document_id": self._document_id,
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "lifetime_ms": round((perf_counter() - self._opened_at) * 1000.0, 3),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        logger.info(
            "Reader document close finished",
            extra={
                "event": "reader.document.close.finished",
                "category": "reader",
                "status": "finished",
                "document_id": self._document_id,
                "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                "lifetime_ms": round((perf_counter() - self._opened_at) * 1000.0, 3),
            },
        )


class ReaderDocumentRuntime:
    """Open Core reader sessions behind an Application-owned document port."""

    def __init__(
        self,
        session_service: ReaderSessionService,
        *,
        document_cache_key: str | None = None,
        archive_extraction_cache: ArchiveExtractionCache | None = None,
        hash_service: HashService | None = None,
        purge_encrypted_cache_on_close: bool = True,
    ) -> None:
        self._session_service = session_service
        self._archive_extraction_cache = archive_extraction_cache
        self._hash_service = hash_service
        self._purge_encrypted_cache_on_close = bool(purge_encrypted_cache_on_close)
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
        # Only the top-level container's own password makes the *document*
        # encrypted. The dict also carries nested-archive passwords, and a
        # plain zip with one protected extra inside must not have its whole
        # (mostly ordinary) cache treated as sensitive -- nested encrypted
        # pages never reach the pool anyway (the scanner strips their
        # `allow_persistent_cache`), so there is nothing there to protect.
        encrypted = str(path) in passwords
        lease = self._new_cache_lease(path, encrypted=encrypted)
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
            needs_extraction_pool=lease is not None,
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

    def _new_cache_lease(self, source_path: Path, *, encrypted: bool) -> ArchiveCacheLease | None:
        """A pool lease for documents whose pages are expensive to read.

        Expense comes from the container format or from encryption, and only
        the first is visible in the file name. The password the caller supplied
        is the earliest evidence of the second -- the scan that would prove it
        per page has not run yet, and a document with no lease can never
        acquire one later.

        Erring toward creating the lease is safe and refusing it is not: an
        unwanted lease costs a pool pin the session never writes through, while
        a missing one silently downgrades the document to on-demand reads for
        its whole life.
        """

        if self._archive_extraction_cache is None:
            return None
        by_format = source_path.suffix.lower() in EXPENSIVE_ARCHIVE_EXTENSIONS
        if not by_format and not encrypted:
            return None
        # Scope follows where the document lives, not whether it is encrypted:
        # a managed encrypted book caches persistently like any other. Either
        # way the extracted bytes land on disk as plaintext -- see
        # ``ArchiveSession._record_is_persistable``.
        scope = ArchiveCacheScope.PERSISTENT if self._managed else ArchiveCacheScope.EPHEMERAL
        lease = ArchiveCacheLease(
            self._archive_extraction_cache,
            self._initial_cache_key,
            scope,
        )
        if encrypted and self._purge_encrypted_cache_on_close:
            # Marked on the pool rather than expressed as an ephemeral scope,
            # because the warmup opens this same document under a lease of its
            # own that knows nothing about this preference. The pool outlives
            # both, so it is the only place the decision holds for all of them.
            #
            # After the lease exists, so the mark is never left behind waiting
            # for a release that no lease will ever make.
            #
            # Called directly, not through a getattr guard: the protocol
            # declares this method, and a cache that lacks it should fail
            # loudly here rather than silently keep plaintext it promised
            # to delete.
            self._archive_extraction_cache.mark_session_scoped(self._initial_cache_key)
        return lease


def _file_change_token(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_size), int(stat.st_mtime_ns), int(getattr(stat, "st_ino", 0))


def _legacy_depth_limit(value: int | None) -> int:
    return -1 if value is None else value
