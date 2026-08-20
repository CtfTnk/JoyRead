"""Thumbnail and cover generation for archive-backed books."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
import re
from threading import Event, RLock
from time import perf_counter
from typing import Protocol

from joyread.core.archive import ArchiveError, ArchiveImageService, ArchiveOpenLimits
from joyread.core.archive.service import ARCHIVE_EXTENSIONS, EXPENSIVE_ARCHIVE_EXTENSIONS
from joyread.core.diagnostics import cache_identity_kind
from joyread.core.models.book import Book
from joyread.core.reader import ReaderImageSession, ReaderSessionService
from joyread.core.reader.pdf import PDF_EXTENSIONS, PdfError
from joyread.core.services.cache_service import (
    BoundedByteCache,
    CacheService,
    ThumbnailCacheClient,
    ThumbnailSourceIdentity,
)
from joyread.infrastructure.filesystem.path_service import PathService


# Image-paged formats only: EPUB lives in ``SUPPORTED_READER_EXTENSIONS``
# (the reader can open it) but its chapter-flow surface has no first
# image we can render a cover from, so it's excluded here.
_THUMBNAIL_GENERABLE_EXTENSIONS = ARCHIVE_EXTENSIONS | PDF_EXTENSIONS


SizeTuple = tuple[int, int]
DetailThumbnailCache = BoundedByteCache[tuple[int, int, int], bytes]

logger = logging.getLogger(__name__)


class ThumbnailRenderer(Protocol):
    """Decode and render fixed-size thumbnail PNGs outside the GUI thread."""

    def render_encoded(self, image_bytes: bytes, size: SizeTuple) -> bytes: ...

    def render_prepared(self, frame: object, size: SizeTuple) -> bytes: ...

    def render_cover_crop(
        self,
        image_bytes: bytes,
        crop_state: "CoverCropState",
        size: SizeTuple,
    ) -> bytes: ...


class ThumbnailRenderError(OSError):
    """Infrastructure image decoding or rendering failed in a controlled way."""


@dataclass(frozen=True)
class DetailThumbnailItem:
    page_index: int
    image_bytes: bytes


@dataclass(frozen=True)
class DetailThumbnailBatch:
    book_uuid: str
    start_index: int
    next_index: int
    has_more: bool
    items: tuple[DetailThumbnailItem, ...]


@dataclass
class _ThumbnailSessionEntry:
    session: ReaderImageSession
    ref_count: int = 1
    active_reads: int = 0
    retired: bool = False
    closed: bool = False


class ThumbnailSourceHandle:
    """Reference-counted access to one shared thumbnail document session."""

    def __init__(
        self,
        owner: "ThumbnailService",
        registry_key: str,
        entry: _ThumbnailSessionEntry,
        *,
        source_id: str,
        suffix: str,
        source_path: Path,
        archive_limits: ArchiveOpenLimits,
        persistent_cache_key: str | None,
    ) -> None:
        self._owner = owner
        self._registry_key = registry_key
        self._entry = entry
        self.source_id = source_id
        self.page_count = max(0, int(entry.session.page_count))
        self.suffix = suffix
        self.source_path = source_path
        self.archive_limits = archive_limits
        self.nested_archive_max_depth = archive_limits.nested_archive_max_depth
        self.archive_global_file_max_depth = archive_limits.global_file_max_depth
        self.persistent_cache_key = persistent_cache_key
        self._closed = False
        self._close_lock = RLock()

    @property
    def access_mode(self):  # noqa: ANN201 - archive enum or absent for PDF.
        return getattr(self._entry.session, "access_mode", None)

    @property
    def requires_sequential_warmup(self) -> bool:
        return bool(getattr(self._entry.session, "requires_sequential_warmup", False))

    @property
    def is_closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def preferred_batch_size(self, page_index: int) -> int:
        provider = getattr(self._entry.session, "thumbnail_batch_size", None)
        if callable(provider):
            return max(1, min(8, int(provider(page_index))))
        return 8 if self.suffix in EXPENSIVE_ARCHIVE_EXTENSIONS else 1

    def plan_read_batch(self, candidates: tuple[int, ...]) -> tuple[int, ...]:
        provider = getattr(self._entry.session, "plan_read_batch", None)
        if callable(provider):
            return tuple(int(index) for index in provider(candidates))
        if not candidates:
            return ()
        return candidates[: self.preferred_batch_size(candidates[0])]

    def read_pages(self, page_indices: Iterable[int]) -> list[object | None]:
        return self._owner._read_thumbnail_pages(self, tuple(page_indices), None)

    def read_thumbnail_pages(
        self,
        page_indices: Iterable[int],
        size: SizeTuple,
    ) -> list[object | None]:
        """Return target-prepared frames when the document backend supports them."""

        return self._owner._read_thumbnail_pages(self, tuple(page_indices), size)

    def read_page(self, page_index: int):  # noqa: ANN201 - archive/PDF page object.
        pages = self.read_pages((page_index,))
        return pages[0] if pages else None

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._owner._release_thumbnail_source(self)

    def __enter__(self) -> "ThumbnailSourceHandle":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:  # noqa: ANN001
        self.close()


@dataclass(frozen=True)
class CoverCropState:
    """Cover transform with pan stored as normalized available travel."""

    source_id: str
    zoom_percent: float
    # Both axes are constrained to [-1, 1]. Zero is centered and the
    # extrema mean the image has reached that edge of its available travel.
    pan_x: float
    pan_y: float
    crop_size: SizeTuple


class ThumbnailService:
    """Generates cached cover files and transient detail-page thumbnails."""

    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

    def __init__(
        self,
        paths: PathService,
        archive_service: ArchiveImageService,
        cache_service: CacheService,
        reader_session_service: ReaderSessionService | None = None,
        *,
        nested_archive_max_depth: int = 2,
        archive_global_file_max_depth: int = 100,
        archive_limits: ArchiveOpenLimits | None = None,
        thumbnail_renderer: ThumbnailRenderer,
    ) -> None:
        self._paths = paths
        self._archive_service = archive_service
        self._reader_session_service = reader_session_service or ReaderSessionService(archive_service)
        self._cache_service = cache_service
        self._thumbnail_renderer = thumbnail_renderer
        self._archive_limits = archive_limits or ArchiveOpenLimits(
            nested_archive_max_depth=_core_depth_limit(
                nested_archive_max_depth,
                default=2,
                maximum=5,
            ),
            global_file_max_depth=_core_depth_limit(
                archive_global_file_max_depth,
                default=100,
                maximum=1000,
            ),
        )
        self._session_entries: dict[str, _ThumbnailSessionEntry] = {}
        self._opening_events: dict[str, Event] = {}
        self._session_registry_lock = RLock()
        self._session_generation = 0
        self._closed = False

    def set_archive_depth_limits(self, nested_max_depth: int, global_file_max_depth: int) -> None:
        self.set_archive_open_limits(
            ArchiveOpenLimits(
                nested_archive_max_depth=_core_depth_limit(nested_max_depth, default=2, maximum=5),
                global_file_max_depth=_core_depth_limit(global_file_max_depth, default=100, maximum=1000),
                max_source_bytes=self._archive_limits.max_source_bytes,
                max_extracted_item_bytes=self._archive_limits.max_extracted_item_bytes,
                max_operation_bytes=self._archive_limits.max_operation_bytes,
                max_image_pixels=self._archive_limits.max_image_pixels,
                external_command_timeout_seconds=self._archive_limits.external_command_timeout_seconds,
            )
        )

    def set_archive_open_limits(self, limits: ArchiveOpenLimits) -> None:
        with self._session_registry_lock:
            if limits == self._archive_limits:
                return
            self._archive_limits = limits
            self._session_generation += 1
            sessions = self._retire_all_sessions_locked()
        _close_sessions(sessions)
        logger.info(
            "Thumbnail source policy changed",
            extra={
                "event": "thumbnail.session.policy_changed",
                "category": "thumbnail",
                "status": "finished",
                "count": len(sessions),
            },
        )

    def close(self) -> None:
        """Release cached document sessions and their archive cache leases."""

        with self._session_registry_lock:
            if self._closed:
                return
            self._closed = True
            self._session_generation += 1
            sessions = self._retire_all_sessions_locked()
        _close_sessions(sessions)
        logger.info(
            "Thumbnail service closed",
            extra={
                "event": "thumbnail.service.closed",
                "category": "thumbnail",
                "status": "finished",
                "count": len(sessions),
            },
        )

    def issue_thumbnail_cache_client(self, client_id: str) -> ThumbnailCacheClient:
        """Lease the app-wide rendered-thumbnail cache for one viewport."""

        return self._cache_service.issue_thumbnail_client(client_id)

    def open_thumbnail_source(self, book: Book) -> ThumbnailSourceHandle | None:
        """Open one reusable source without decoding any page pixels."""

        if not self.can_generate_from(book):
            return None
        document_cache_key = self._document_cache_key(book)
        registry_key = self._session_registry_key(document_cache_key)
        entry = self._acquire_thumbnail_session(book, document_cache_key, registry_key)
        source_id = self._thumbnail_source_id(document_cache_key)
        return ThumbnailSourceHandle(
            self,
            registry_key,
            entry,
            source_id=source_id,
            suffix=Path(book.file_path).suffix.lower(),
            source_path=Path(book.file_path),
            archive_limits=self._archive_limits,
            persistent_cache_key=document_cache_key if book.file_id is not None else None,
        )

    def stream_thumbnails(
        self,
        source: ThumbnailSourceHandle,
        page_indices: Iterable[int],
        size: SizeTuple,
        emit_item: Callable[[DetailThumbnailItem], None],
    ) -> None:
        """Read a bounded group and emit each rendered item independently."""

        requested = tuple(
            dict.fromkeys(
                int(index)
                for index in page_indices
                if 0 <= int(index) < source.page_count
            )
        )
        if not requested:
            return
        pages = source.read_thumbnail_pages(requested, size)
        for page in pages:
            if page is None:
                continue
            page_index = int(getattr(page, "index", getattr(page, "page_index", -1)))
            try:
                rendered = self._render_thumbnail_page(page, size)
            except (OSError, ThumbnailRenderError) as exc:
                logger.warning("Thumbnail stream render failed page=%d: %s", page_index, exc)
                continue
            emit_item(DetailThumbnailItem(page_index, rendered))

    def can_generate_from(self, book: Book) -> bool:
        source = Path(book.file_path)
        return (
            source.exists()
            and source.is_file()
            and source.suffix.lower() in _THUMBNAIL_GENERABLE_EXTENSIONS
        )

    def existing_cover_path(self, book: Book, size: SizeTuple) -> Path | None:
        explicit = self._explicit_cover_path(book, size)
        if explicit is not None:
            logger.debug("Cover path resolved from explicit DB value book=%s path=%s", book.uuid, explicit)
            return explicit

        if not self.can_generate_from(book):
            # Source is missing (or unreadable). Check the fallback
            # cache first so repeated shelf renders skip the glob; the
            # entry is only re-resolved when the cached path no longer
            # exists on disk.
            fallback_key = self._cover_fallback_key(book, size)
            cached_fallback = self._cache_service.cover_index.get(fallback_key)
            if cached_fallback:
                cached_path = Path(cached_fallback)
                if cached_path.exists():
                    logger.debug("Cover fallback cache hit book=%s path=%s", book.uuid, cached_path)
                    return cached_path
            resolved = self._fallback_cover_path(book, size)
            if resolved is not None:
                self._cache_service.cover_index.put(fallback_key, str(resolved))
                logger.debug("Cover fallback resolved book=%s path=%s", book.uuid, resolved)
            return resolved

        cache_key = self._cover_cache_key(book, size)
        cached = self._cache_service.cover_index.get(cache_key)
        if cached:
            cached_path = Path(cached)
            if cached_path.exists():
                logger.debug("Cover cache hit book=%s path=%s", book.uuid, cached_path)
                return cached_path

        cover_path = self._cover_path(book, size)
        if cover_path.exists():
            self._cache_service.cover_index.put(cache_key, str(cover_path))
            logger.debug("Cover file exists book=%s path=%s", book.uuid, cover_path)
            return cover_path
        return None

    def load_cover_source_page(self, book: Book, page_index: int) -> bytes | None:
        if page_index < 0 or not self.can_generate_from(book):
            return None

        try:
            source = self.open_thumbnail_source(book)
            if source is None:
                return None
            with source:
                page = source.read_page(page_index)
        except (ArchiveError, PdfError, OSError) as exc:
            logger.warning("Cover source load failed for book=%s page=%d: %s", book.uuid, page_index, exc)
            return None
        return None if page is None else page.image_bytes

    def save_edited_cover(
        self,
        book: Book,
        source_bytes: bytes,
        crop_state: CoverCropState,
        size: SizeTuple,
    ) -> Path:
        rendered = self._thumbnail_renderer.render_cover_crop(source_bytes, crop_state, size)
        cover_path = self._editable_cover_path(book, size)
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(rendered)
        self.invalidate_cover_cache(book.uuid)
        return cover_path

    def invalidate_cover_cache(self, book_uuid: str) -> int:
        return self._cache_service.cover_index.purge(
            lambda key: (
                key.startswith(f"cover:{book_uuid}:")
                or key.startswith(f"cover-fallback:{book_uuid}:")
                or key.startswith(f"cover-explicit:{book_uuid}:")
            )
        )

    def invalidate_file_cache(self, file_id: str) -> None:
        """Forget sessions and rendered thumbnails derived from managed content."""

        document_cache_key = f"file:{file_id}"
        with self._session_registry_lock:
            sessions: list[ReaderImageSession] = []
            for key in tuple(self._session_entries):
                if key.startswith(f"session:{document_cache_key}:"):
                    entry = self._session_entries.pop(key)
                    entry.retired = True
                    session = self._close_entry_if_idle_locked(entry)
                    if session is not None:
                        sessions.append(session)
        _close_sessions(sessions)
        self._cache_service.purge_thumbnail_source(document_cache_key)

    def generate_cover(self, book: Book, size: SizeTuple) -> Path | None:
        logger.debug("Generate cover requested book=%s size=%s", book.uuid, size)
        existing = self.existing_cover_path(book, size)
        if existing is not None:
            return existing
        if not self.can_generate_from(book):
            logger.debug("Generate cover skipped book=%s: unsupported or missing source", book.uuid)
            return None

        try:
            source = self.open_thumbnail_source(book)
            if source is None:
                return None
            with source:
                pages = source.read_thumbnail_pages((0,), size)
                first_page = pages[0] if pages else None
                if first_page is None:
                    return None
                rendered = self._render_thumbnail_page(first_page, size)
        except (ArchiveError, PdfError, OSError, ThumbnailRenderError) as exc:
            logger.warning(
                "Cover generation failed for book=%s size=%s: %s",
                book.uuid,
                size,
                exc,
            )
            return None

        cover_path = self._cover_path(book, size)
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(rendered)
        self._remove_stale_covers(book, keep=cover_path)
        self._cache_service.cover_index.put(self._cover_cache_key(book, size), str(cover_path))
        logger.debug("Generate cover complete book=%s path=%s", book.uuid, cover_path)
        return cover_path

    def generate_page_thumbnail(
        self,
        book: Book,
        page_index: int,
        size: SizeTuple,
        *,
        detail_cache: DetailThumbnailCache | None = None,
    ) -> bytes | None:
        if page_index < 0 or not self.can_generate_from(book):
            return None

        cache_key = self._detail_cache_key(page_index, size)
        if detail_cache is not None:
            cached = detail_cache.get(cache_key)
            if cached is not None:
                logger.debug("Page thumbnail cache hit book=%s page=%d size=%s", book.uuid, page_index, size)
                return cached

        try:
            source = self.open_thumbnail_source(book)
            if source is None:
                return None
            with source:
                pages = source.read_thumbnail_pages((page_index,), size)
                page = pages[0] if pages else None
                if page is None:
                    return None
                rendered = self._render_thumbnail_page(page, size)
        except (ArchiveError, PdfError, OSError, ThumbnailRenderError) as exc:
            logger.warning(
                "Page thumbnail failed for book=%s page=%d size=%s: %s",
                book.uuid,
                page_index,
                size,
                exc,
            )
            return None

        if detail_cache is not None:
            detail_cache.put(cache_key, rendered)
        return rendered

    def generate_detail_thumbnail_batch(
        self,
        book: Book,
        start_index: int,
        batch_size: int = 14,
        size: SizeTuple = (100, 142),
        *,
        detail_cache: DetailThumbnailCache | None = None,
    ) -> DetailThumbnailBatch:
        start_index = max(0, start_index)
        batch_size = max(1, batch_size)
        logger.debug(
            "Detail thumbnail batch requested book=%s start=%d size=%d target_size=%s",
            book.uuid,
            start_index,
            batch_size,
            size,
        )
        empty = DetailThumbnailBatch(
            book_uuid=book.uuid,
            start_index=start_index,
            next_index=start_index,
            has_more=False,
            items=(),
        )
        if not self.can_generate_from(book):
            return empty

        try:
            source = self.open_thumbnail_source(book)
        except (ArchiveError, PdfError, OSError) as exc:
            logger.warning(
                "Detail thumbnail session failed for book=%s: %s", book.uuid, exc
            )
            return empty

        if source is None:
            return empty

        with source:
            if start_index >= source.page_count:
                logger.debug(
                    "Detail thumbnail batch empty book=%s start=%d page_count=%d",
                    book.uuid,
                    start_index,
                    source.page_count,
                )
                return empty

            items: list[DetailThumbnailItem] = []
            page_index = start_index
            end_index = min(source.page_count, start_index + batch_size)
            pages = source.read_thumbnail_pages(range(start_index, end_index), size)
            for page in pages:
                if page is None:
                    page_index += 1
                    continue
                rendered_index = int(getattr(page, "index", getattr(page, "page_index", page_index)))
                rendered = self._render_detail_thumbnail(rendered_index, page, size, detail_cache)
                if rendered is not None:
                    items.append(DetailThumbnailItem(page_index=rendered_index, image_bytes=rendered))
                page_index += 1

        batch = DetailThumbnailBatch(
            book_uuid=book.uuid,
            start_index=start_index,
            next_index=end_index,
            has_more=end_index < source.page_count,
            items=tuple(items),
        )
        logger.debug(
            "Detail thumbnail batch complete book=%s start=%d next=%d rendered=%d has_more=%s",
            book.uuid,
            start_index,
            end_index,
            len(items),
            batch.has_more,
        )
        return batch

    def _render_detail_thumbnail(
        self,
        page_index: int,
        page: object,
        size: SizeTuple,
        detail_cache: DetailThumbnailCache | None,
    ) -> bytes | None:
        cache_key = self._detail_cache_key(page_index, size)
        if detail_cache is not None:
            cached = detail_cache.get(cache_key)
            if cached is not None:
                logger.debug("Detail thumbnail cache hit page=%d size=%s", page_index, size)
                return cached

        try:
            rendered = self._render_thumbnail_page(page, size)
        except (OSError, ThumbnailRenderError) as exc:
            logger.warning("Detail thumbnail render failed for page=%d: %s", page_index, exc)
            return None

        if detail_cache is not None:
            detail_cache.put(cache_key, rendered)
        return rendered

    def _document_cache_key(self, book: Book) -> str:
        """Return stable content identity without stat-ing a managed file."""

        if book.file_id:
            return f"file:{book.file_id}"
        # Mock/legacy book rows without a file id must still avoid source path
        # metadata. They receive a book-scoped cache namespace until migrated.
        return f"book:{book.uuid}"

    def _open_thumbnail_session(
        self,
        book: Book,
        document_cache_key: str,
        limits: ArchiveOpenLimits,
    ) -> ReaderImageSession:
        logger.debug("Thumbnail session open book=%s source=%s", book.uuid, book.file_path)
        allow_extraction_cache = (
            book.file_id is not None
            and Path(book.file_path).suffix.lower() in EXPENSIVE_ARCHIVE_EXTENSIONS
        )
        try:
            session = self._reader_session_service.open_document(
                book.file_path,
                limits=limits,
                document_cache_key=document_cache_key,
                allow_persistent_cache=allow_extraction_cache,
            )
        except TypeError as exc:
            if "document_cache_key" not in str(exc) and "allow_persistent_cache" not in str(exc):
                raise
            session = self._reader_session_service.open_document(book.file_path, limits=limits)
        return session

    def _acquire_thumbnail_session(
        self,
        book: Book,
        document_cache_key: str,
        registry_key: str,
    ) -> _ThumbnailSessionEntry:
        while True:
            with self._session_registry_lock:
                if self._closed:
                    raise RuntimeError("ThumbnailService is closed")
                entry = self._session_entries.get(registry_key)
                if entry is not None and not entry.retired:
                    entry.ref_count += 1
                    logger.debug(
                        "Thumbnail source session reused",
                        extra={
                            "event": "thumbnail.session.reused",
                            "category": "thumbnail",
                            "identity_kind": cache_identity_kind(document_cache_key),
                            "count": entry.ref_count,
                        },
                    )
                    return entry
                event = self._opening_events.get(registry_key)
                owner = event is None
                if owner:
                    event = Event()
                    self._opening_events[registry_key] = event
                    generation = self._session_generation
                    limits = self._archive_limits
            assert event is not None
            if not owner:
                event.wait()
                continue
            break

        started = perf_counter()
        logger.debug(
            "Thumbnail source session opening",
            extra={
                "event": "thumbnail.session.open.started",
                "category": "thumbnail",
                "status": "started",
                "book_id": book.uuid,
                "identity_kind": cache_identity_kind(document_cache_key),
            },
        )
        try:
            session = self._open_thumbnail_session(book, document_cache_key, limits)
        except Exception as exc:
            with self._session_registry_lock:
                self._opening_events.pop(registry_key, None)
                event.set()
            logger.error(
                "Thumbnail source session failed to open",
                exc_info=True,
                extra={
                    "event": "thumbnail.session.open.failed",
                    "category": "thumbnail",
                    "status": "failed",
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "book_id": book.uuid,
                    "identity_kind": cache_identity_kind(document_cache_key),
                    "error_type": type(exc).__name__,
                },
            )
            raise

        with self._session_registry_lock:
            accepted = (
                not self._closed
                and generation == self._session_generation
                and limits == self._archive_limits
            )
            if accepted:
                entry = _ThumbnailSessionEntry(session)
                self._session_entries[registry_key] = entry
            self._opening_events.pop(registry_key, None)
            event.set()
        if not accepted:
            _close_sessions((session,))
            logger.info(
                "Thumbnail source session discarded after policy change",
                extra={
                    "event": "thumbnail.session.open.discarded",
                    "category": "thumbnail",
                    "status": "cancelled",
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "book_id": book.uuid,
                    "identity_kind": cache_identity_kind(document_cache_key),
                },
            )
            raise RuntimeError("Thumbnail source policy changed while opening")
        logger.debug(
            "Thumbnail source session opened",
            extra={
                "event": "thumbnail.session.open.finished",
                "category": "thumbnail",
                "status": "finished",
                "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                "book_id": book.uuid,
                "identity_kind": cache_identity_kind(document_cache_key),
                "page_count": int(getattr(session, "page_count", 0)),
            },
        )
        return entry

    def _read_thumbnail_pages(
        self,
        handle: ThumbnailSourceHandle,
        page_indices: tuple[int, ...],
        target_size: SizeTuple | None,
    ) -> list[object | None]:
        entry = handle._entry
        with self._session_registry_lock:
            if handle._closed or entry.retired or entry.closed:
                return [None for _index in page_indices]
            entry.active_reads += 1
        try:
            prepare = getattr(entry.session, "prepare_thumbnail_pages", None)
            if target_size is not None and callable(prepare):
                return list(prepare(page_indices, target_size))
            read = getattr(entry.session, "read_pages", None)
            if not callable(read):
                read = entry.session.get_pages
            return list(read(page_indices))
        finally:
            session: ReaderImageSession | None = None
            with self._session_registry_lock:
                entry.active_reads = max(0, entry.active_reads - 1)
                if entry.ref_count == 0 or entry.retired:
                    session = self._close_entry_if_idle_locked(entry)
            if session is not None:
                _close_sessions((session,))

    def _release_thumbnail_source(self, handle: ThumbnailSourceHandle) -> None:
        session: ReaderImageSession | None = None
        with self._session_registry_lock:
            entry = handle._entry
            entry.ref_count = max(0, entry.ref_count - 1)
            if entry.ref_count == 0:
                if self._session_entries.get(handle._registry_key) is entry:
                    self._session_entries.pop(handle._registry_key, None)
                entry.retired = True
                session = self._close_entry_if_idle_locked(entry)
            logger.debug(
                "Thumbnail source lease released",
                extra={
                    "event": "thumbnail.session.lease_released",
                    "category": "thumbnail",
                    "status": "closing" if entry.ref_count == 0 else "active",
                    "count": entry.ref_count,
                    "active_reads": entry.active_reads,
                },
            )
        if session is not None:
            _close_sessions((session,))

    def _retire_all_sessions_locked(self) -> tuple[ReaderImageSession, ...]:
        sessions: list[ReaderImageSession] = []
        entries = tuple(self._session_entries.values())
        self._session_entries.clear()
        for entry in entries:
            entry.retired = True
            session = self._close_entry_if_idle_locked(entry)
            if session is not None:
                sessions.append(session)
        return tuple(sessions)

    @staticmethod
    def _close_entry_if_idle_locked(entry: _ThumbnailSessionEntry) -> ReaderImageSession | None:
        if entry.closed or entry.active_reads > 0:
            return None
        entry.closed = True
        return entry.session

    def _session_registry_key(self, document_cache_key: str) -> str:
        return f"session:{document_cache_key}:{self._archive_limits.cache_signature()}"

    def _thumbnail_source_id(self, document_cache_key: str) -> str:
        return ThumbnailSourceIdentity(
            document_cache_key,
            self._archive_limits.cache_signature(),
        ).cache_id

    def _render_thumbnail_page(self, page: object, size: SizeTuple) -> bytes:
        frame = getattr(page, "frame", None)
        if frame is not None:
            return self._thumbnail_renderer.render_prepared(frame, size)
        image_bytes = getattr(page, "image_bytes", None)
        if not isinstance(image_bytes, bytes):
            raise ThumbnailRenderError("Thumbnail page has no renderable payload")
        return self._thumbnail_renderer.render_encoded(image_bytes, size)

    def _cover_path(self, book: Book, size: SizeTuple) -> Path:
        return self._covers_dir() / f"{self._safe_book_uuid(book.uuid)}-generated-{size[0]}x{size[1]}.png"

    def _explicit_cover_path(self, book: Book, size: SizeTuple) -> Path | None:
        if not book.cover_thumbnail_path:
            return None
        cache_key = self._cover_explicit_key(book, size)
        cached = self._cache_service.cover_index.get(cache_key)
        if cached:
            cached_path = Path(cached)
            if cached_path.exists():
                return cached_path

        cover_path = Path(book.cover_thumbnail_path)
        if not cover_path.exists():
            return None
        self._cache_service.cover_index.put(cache_key, str(cover_path))
        return cover_path

    def _editable_cover_path(self, book: Book, size: SizeTuple) -> Path:
        if book.cover_thumbnail_path:
            current = Path(book.cover_thumbnail_path)
            if self._is_managed_cover_path(current):
                return current
        return self._custom_cover_path(book, size)

    def _custom_cover_path(self, book: Book, size: SizeTuple) -> Path:
        return self._covers_dir() / f"{self._safe_book_uuid(book.uuid)}-custom-{size[0]}x{size[1]}.png"

    def _is_managed_cover_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self._covers_dir().resolve())
        except (OSError, ValueError):
            return False
        return True

    def _covers_dir(self) -> Path:
        return self._paths.paths.thumbnails / "covers"

    def _fallback_cover_path(self, book: Book, size: SizeTuple) -> Path | None:
        safe_uuid = self._safe_book_uuid(book.uuid)
        pattern = f"{safe_uuid}-*-{size[0]}x{size[1]}.png"
        candidates = list(self._covers_dir().glob(pattern))
        if not candidates:
            return None
        try:
            return max(candidates, key=lambda path: path.stat().st_mtime)
        except OSError as exc:
            logger.debug(
                "thumbnail fallback stat failed book=%s, picking first candidate: %s",
                book.uuid,
                exc,
            )
            return candidates[0]

    def _remove_stale_covers(self, book: Book, keep: Path) -> None:
        safe_uuid = self._safe_book_uuid(book.uuid)
        for path in self._covers_dir().glob(f"{safe_uuid}-generated-*.png"):
            if path != keep:
                path.unlink(missing_ok=True)

    def _cover_cache_key(self, book: Book, size: SizeTuple) -> str:
        return f"cover:{book.uuid}:{size[0]}x{size[1]}"

    def _cover_fallback_key(self, book: Book, size: SizeTuple) -> str:
        # Distinct prefix so the live-cover path's
        # ``cover:<uuid>:<size>`` keys never collide with
        # the missing-source fallback record.
        return f"cover-fallback:{book.uuid}:{size[0]}x{size[1]}"

    def _cover_explicit_key(self, book: Book, size: SizeTuple) -> str:
        return f"cover-explicit:{book.uuid}:{size[0]}x{size[1]}"

    @staticmethod
    def _detail_cache_key(page_index: int, size: SizeTuple) -> tuple[int, int, int]:
        # Detail caches are per-book and live with the detail viewmodel, so we
        # only need ``(page_index, width, height)`` to disambiguate.
        return page_index, int(size[0]), int(size[1])

    def _safe_book_uuid(self, book_uuid: str) -> str:
        return self._SAFE_NAME_RE.sub("_", book_uuid).strip("_") or "book"


def _normalize_depth_limit(value: object, *, default: int, maximum: int) -> int:
    try:
        depth = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if depth == -1:
        return -1
    if depth < 1:
        return default
    return min(maximum, depth)


def _core_depth_limit(value: object, *, default: int, maximum: int) -> int | None:
    normalized = _normalize_depth_limit(value, default=default, maximum=maximum)
    return None if normalized == -1 else normalized


def _close_sessions(sessions: Iterable[ReaderImageSession]) -> None:
    for session in sessions:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # Cleanup must continue for sibling sessions.
                logger.error(
                    "Thumbnail source session failed to close",
                    exc_info=True,
                    extra={
                        "event": "thumbnail.session.close.failed",
                        "category": "thumbnail",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    },
                )
