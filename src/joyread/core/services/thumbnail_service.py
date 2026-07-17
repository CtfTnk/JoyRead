"""Thumbnail and cover generation for archive-backed books."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
from threading import Lock, RLock

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from joyread.core.archive import ArchiveError, ArchiveImageService
from joyread.core.archive.service import ARCHIVE_EXTENSIONS, EXPENSIVE_ARCHIVE_EXTENSIONS
from joyread.core.models.book import Book
from joyread.core.reader import ReaderImageSession, ReaderSessionService
from joyread.core.reader.pdf_session import PDF_EXTENSIONS, PdfError
from joyread.core.services.cache_service import BoundedByteCache, CacheService, ThumbnailCacheClient
from joyread.infrastructure.filesystem.path_service import PathService


# Image-paged formats only: EPUB lives in ``SUPPORTED_READER_EXTENSIONS``
# (the reader can open it) but its chapter-flow surface has no first
# image we can render a cover from, so it's excluded here.
_THUMBNAIL_GENERABLE_EXTENSIONS = ARCHIVE_EXTENSIONS | PDF_EXTENSIONS


SizeTuple = tuple[int, int]
DetailThumbnailCache = BoundedByteCache[tuple[int, int, int], bytes]

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class ThumbnailSourceHandle:
    """Source-stable session used by viewport thumbnail streams."""

    source_id: str
    page_count: int
    suffix: str
    session: ReaderImageSession
    access_lock: RLock
    source_path: Path
    nested_archive_max_depth: int
    archive_global_file_max_depth: int

    def preferred_batch_size(self, page_index: int) -> int:
        provider = getattr(self.session, "thumbnail_batch_size", None)
        if callable(provider):
            return max(1, min(8, int(provider(page_index))))
        return 8 if self.suffix in EXPENSIVE_ARCHIVE_EXTENSIONS else 1


@dataclass(frozen=True)
class CoverCropState:
    source_id: str
    zoom_percent: float
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
    ) -> None:
        self._paths = paths
        self._archive_service = archive_service
        self._reader_session_service = reader_session_service or ReaderSessionService(archive_service)
        self._cache_service = cache_service
        self._nested_archive_max_depth = _normalize_depth_limit(
            nested_archive_max_depth,
            default=2,
            maximum=5,
        )
        self._archive_global_file_max_depth = _normalize_depth_limit(
            archive_global_file_max_depth,
            default=100,
            maximum=1000,
        )
        # The session cache keeps an open reader session per book so grid
        # covers and detail-thumbnail batches do not re-scan the source for
        # the same book. Bounded implicitly by how many books are being
        # interacted with concurrently.
        self._session_cache: dict[str, ReaderImageSession] = {}
        self._session_access_locks: dict[str, RLock] = {}
        self._session_cache_lock = Lock()

    def set_archive_depth_limits(self, nested_max_depth: int, global_file_max_depth: int) -> None:
        nested = _normalize_depth_limit(nested_max_depth, default=2, maximum=5)
        global_depth = _normalize_depth_limit(global_file_max_depth, default=100, maximum=1000)
        with self._session_cache_lock:
            if (
                nested == self._nested_archive_max_depth
                and global_depth == self._archive_global_file_max_depth
            ):
                return
            self._nested_archive_max_depth = nested
            self._archive_global_file_max_depth = global_depth
            self._session_cache.clear()
            self._session_access_locks.clear()

    def issue_thumbnail_cache_client(self, client_id: str) -> ThumbnailCacheClient:
        """Lease the app-wide rendered-thumbnail cache for one viewport."""

        return self._cache_service.issue_thumbnail_client(client_id)

    def open_thumbnail_source(self, book: Book) -> ThumbnailSourceHandle | None:
        """Open one reusable source without decoding any page pixels."""

        if not self.can_generate_from(book):
            return None
        signature = self._source_signature(book)
        if signature is None:
            return None
        session = self._session_for(book, signature)
        source_id = self._thumbnail_source_id(book, signature)
        with self._session_cache_lock:
            access_lock = self._session_access_locks.setdefault(source_id, RLock())
        return ThumbnailSourceHandle(
            source_id=source_id,
            page_count=session.page_count,
            suffix=Path(book.file_path).suffix.lower(),
            session=session,
            access_lock=access_lock,
            source_path=Path(book.file_path),
            nested_archive_max_depth=self._nested_archive_max_depth,
            archive_global_file_max_depth=self._archive_global_file_max_depth,
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
        with source.access_lock:
            pages = source.session.get_pages(requested)
        for page in pages:
            if page is None:
                continue
            page_index = int(getattr(page, "index", getattr(page, "page_index", -1)))
            try:
                rendered = render_contain_blur_thumbnail(page.image_bytes, size)
            except (OSError, UnidentifiedImageError) as exc:
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

        signature = self._source_signature(book)
        if signature is None:
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

        cache_key = self._cover_cache_key(book, signature, size)
        cached = self._cache_service.cover_index.get(cache_key)
        if cached:
            cached_path = Path(cached)
            if cached_path.exists():
                logger.debug("Cover cache hit book=%s path=%s", book.uuid, cached_path)
                return cached_path

        cover_path = self._cover_path(book, signature, size)
        if cover_path.exists():
            self._cache_service.cover_index.put(cache_key, str(cover_path))
            logger.debug("Cover file exists book=%s path=%s", book.uuid, cover_path)
            return cover_path
        return None

    def load_cover_source_page(self, book: Book, page_index: int) -> bytes | None:
        if page_index < 0 or not self.can_generate_from(book):
            return None

        signature = self._source_signature(book)
        if signature is None:
            return None

        try:
            session = self._session_for(book, signature)
            source_id = self._thumbnail_source_id(book, signature)
            with self._session_cache_lock:
                access_lock = self._session_access_locks.setdefault(source_id, RLock())
            with access_lock:
                page = session.get_page(page_index)
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
        rendered = render_cover_crop(source_bytes, crop_state, size)
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

    def generate_cover(self, book: Book, size: SizeTuple) -> Path | None:
        logger.debug("Generate cover requested book=%s size=%s", book.uuid, size)
        existing = self.existing_cover_path(book, size)
        if existing is not None:
            return existing
        if not self.can_generate_from(book):
            logger.debug("Generate cover skipped book=%s: unsupported or missing source", book.uuid)
            return None

        signature = self._source_signature(book)
        if signature is None:
            logger.debug("Generate cover skipped book=%s: source signature unavailable", book.uuid)
            return None

        try:
            session = self._session_for(book, signature)
            first_page = session.get_page(0)
            if first_page is None:
                return None
            rendered = render_contain_blur_thumbnail(first_page.image_bytes, size)
        except (ArchiveError, PdfError, OSError, UnidentifiedImageError) as exc:
            logger.warning(
                "Cover generation failed for book=%s size=%s: %s",
                book.uuid,
                size,
                exc,
            )
            return None

        cover_path = self._cover_path(book, signature, size)
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(rendered)
        self._remove_stale_covers(book, keep=cover_path)
        self._cache_service.cover_index.put(self._cover_cache_key(book, signature, size), str(cover_path))
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

        signature = self._source_signature(book)
        if signature is None:
            return None

        cache_key = self._detail_cache_key(page_index, size)
        if detail_cache is not None:
            cached = detail_cache.get(cache_key)
            if cached is not None:
                logger.debug("Page thumbnail cache hit book=%s page=%d size=%s", book.uuid, page_index, size)
                return cached

        try:
            session = self._session_for(book, signature)
            page = session.get_page(page_index)
            if page is None:
                return None
            rendered = render_contain_blur_thumbnail(page.image_bytes, size)
        except (ArchiveError, PdfError, OSError, UnidentifiedImageError) as exc:
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

        signature = self._source_signature(book)
        if signature is None:
            return empty

        try:
            session = self._session_for(book, signature)
        except (ArchiveError, PdfError, OSError) as exc:
            logger.warning(
                "Detail thumbnail session failed for book=%s: %s", book.uuid, exc
            )
            return empty

        if start_index >= session.page_count:
            logger.debug(
                "Detail thumbnail batch empty book=%s start=%d page_count=%d",
                book.uuid,
                start_index,
                session.page_count,
            )
            return empty

        items: list[DetailThumbnailItem] = []
        page_index = start_index
        end_index = min(session.page_count, start_index + batch_size)
        pages = session.get_pages(range(start_index, end_index))
        for page in pages:
            if page is None:
                page_index += 1
                continue
            rendered_index = int(getattr(page, "index", getattr(page, "page_index", page_index)))
            rendered = self._render_detail_thumbnail(rendered_index, page.image_bytes, size, detail_cache)
            if rendered is not None:
                items.append(DetailThumbnailItem(page_index=rendered_index, image_bytes=rendered))
            page_index += 1

        batch = DetailThumbnailBatch(
            book_uuid=book.uuid,
            start_index=start_index,
            next_index=end_index,
            has_more=end_index < session.page_count,
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
        page_bytes: bytes,
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
            rendered = render_contain_blur_thumbnail(page_bytes, size)
        except (OSError, UnidentifiedImageError) as exc:
            logger.warning("Detail thumbnail render failed for page=%d: %s", page_index, exc)
            return None

        if detail_cache is not None:
            detail_cache.put(cache_key, rendered)
        return rendered

    def _source_signature(self, book: Book) -> str | None:
        source = Path(book.file_path)
        try:
            stat = source.stat()
        except OSError as exc:
            logger.debug(
                "thumbnail source signature unavailable book=%s path=%s: %s",
                book.uuid,
                source,
                exc,
            )
            return None
        return f"{stat.st_mtime_ns}-{stat.st_size}"

    def _session_for(self, book: Book, signature: str) -> ReaderImageSession:
        with self._session_cache_lock:
            nested_depth = self._nested_archive_max_depth
            global_depth = self._archive_global_file_max_depth
            cache_key = f"session:{book.uuid}:{signature}:{nested_depth}:{global_depth}"
            session = self._session_cache.get(cache_key)
        if session is not None:
            logger.debug("Thumbnail session cache hit book=%s", book.uuid)
            return session

        # Source scanning is expensive for large books, so keep a source-stable
        # session around for cover and detail thumbnail batches.
        logger.debug("Thumbnail session cache miss book=%s source=%s", book.uuid, book.file_path)
        session = self._reader_session_service.open_document(
            book.file_path,
            nested_archive_max_depth=nested_depth,
            archive_global_file_max_depth=global_depth,
        )
        with self._session_cache_lock:
            if (
                nested_depth == self._nested_archive_max_depth
                and global_depth == self._archive_global_file_max_depth
            ):
                self._session_cache[cache_key] = session
        return session

    def _thumbnail_source_id(self, book: Book, signature: str) -> str:
        source = Path(book.file_path)
        try:
            path = str(source.resolve())
        except OSError:
            path = str(source.absolute())
        return (
            f"{path}:{signature}:nested={self._nested_archive_max_depth}:"
            f"global={self._archive_global_file_max_depth}"
        )

    def _cover_path(self, book: Book, signature: str, size: SizeTuple) -> Path:
        return self._covers_dir() / f"{self._safe_book_uuid(book.uuid)}-{signature}-{size[0]}x{size[1]}.png"

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
        for path in self._covers_dir().glob(f"{safe_uuid}-*.png"):
            if path != keep:
                path.unlink(missing_ok=True)

    def _cover_cache_key(self, book: Book, signature: str, size: SizeTuple) -> str:
        return f"cover:{book.uuid}:{signature}:{size[0]}x{size[1]}"

    def _cover_fallback_key(self, book: Book, size: SizeTuple) -> str:
        # Distinct prefix so the live-cover path's
        # ``cover:<uuid>:<signature>:<size>`` keys never collide with
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


def render_contain_blur_thumbnail(image_bytes: bytes, size: SizeTuple) -> bytes:
    """Render a fixed-size cover with full-page foreground and blurred fill.

    This matches the desired cover behavior: never crop the readable page, but
    avoid empty bars by reusing a blurred aspect-fill copy behind it.
    """

    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("Thumbnail size must be positive.")

    with Image.open(BytesIO(image_bytes)) as source_image:
        image = ImageOps.exif_transpose(source_image)
        foreground_source = image.convert("RGBA")
        background_source = image.convert("RGB")

    background = _resize_to_fill(background_source, size)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(8, min(width, height) // 12)))

    foreground = _resize_to_contain(foreground_source, size)
    x = (width - foreground.width) // 2
    y = (height - foreground.height) // 2
    background = background.convert("RGBA")
    background.alpha_composite(foreground, dest=(x, y))

    output = BytesIO()
    background.save(output, format="PNG")
    return output.getvalue()


def render_cover_crop(image_bytes: bytes, crop_state: CoverCropState, size: SizeTuple) -> bytes:
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("Cover size must be positive.")

    with Image.open(BytesIO(image_bytes)) as source_image:
        image = ImageOps.exif_transpose(source_image)
        foreground_source = image.convert("RGBA")
        background_source = image.convert("RGB")

    background = _resize_to_fill(background_source, size)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(8, min(width, height) // 12)))
    background = background.convert("RGBA")

    fill_scale = max(width / foreground_source.width, height / foreground_source.height)
    scale = fill_scale * max(1, crop_state.zoom_percent) / 100.0
    resized = foreground_source.resize(
        (
            max(1, round(foreground_source.width * scale)),
            max(1, round(foreground_source.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )
    x = ((width - resized.width) // 2) + round(crop_state.pan_x)
    y = ((height - resized.height) // 2) + round(crop_state.pan_y)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay.alpha_composite(resized, dest=(x, y))
    background.alpha_composite(overlay)

    output = BytesIO()
    background.save(output, format="PNG")
    return output.getvalue()


def _resize_to_fill(image: Image.Image, size: SizeTuple) -> Image.Image:
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _resize_to_contain(image: Image.Image, size: SizeTuple) -> Image.Image:
    width, height = size
    scale = min(width / image.width, height / image.height)
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)


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
