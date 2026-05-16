"""Thumbnail and cover generation for archive-backed books."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
from threading import Lock

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from joyread.core.archive import ArchiveError, ArchiveImageService
from joyread.core.archive.service import ARCHIVE_EXTENSIONS
from joyread.core.models.book import Book
from joyread.core.reader import ReaderImageSession, ReaderSessionService
from joyread.core.reader.pdf_session import PDF_EXTENSIONS, PdfError
from joyread.core.services.cache_service import BoundedByteCache, CacheService
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


class ThumbnailService:
    """Generates cached cover files and transient detail-page thumbnails."""

    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

    def __init__(
        self,
        paths: PathService,
        archive_service: ArchiveImageService,
        cache_service: CacheService,
        reader_session_service: ReaderSessionService | None = None,
    ) -> None:
        self._paths = paths
        self._archive_service = archive_service
        self._reader_session_service = reader_session_service or ReaderSessionService(archive_service)
        self._cache_service = cache_service
        # The session cache keeps an open reader session per book so grid
        # covers and detail-thumbnail batches do not re-scan the source for
        # the same book. Bounded implicitly by how many books are being
        # interacted with concurrently.
        self._session_cache: dict[str, ReaderImageSession] = {}
        self._session_cache_lock = Lock()

    def can_generate_from(self, book: Book) -> bool:
        source = Path(book.file_path)
        return (
            source.exists()
            and source.is_file()
            and source.suffix.lower() in _THUMBNAIL_GENERABLE_EXTENSIONS
        )

    def existing_cover_path(self, book: Book, size: SizeTuple) -> Path | None:
        signature = self._source_signature(book)
        if signature is None:
            # Source is missing (or unreadable), so fall back to any cached
            # cover on disk to keep the bookshelf visually populated.
            return self._fallback_cover_path(book, size)

        cache_key = self._cover_cache_key(book, signature, size)
        cached = self._cache_service.cover_index.get(cache_key)
        if cached:
            cached_path = Path(cached)
            if cached_path.exists():
                return cached_path

        cover_path = self._cover_path(book, signature, size)
        if cover_path.exists():
            self._cache_service.cover_index.put(cache_key, str(cover_path))
            return cover_path
        return None

    def generate_cover(self, book: Book, size: SizeTuple) -> Path | None:
        existing = self.existing_cover_path(book, size)
        if existing is not None:
            return existing
        if not self.can_generate_from(book):
            return None

        signature = self._source_signature(book)
        if signature is None:
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

        return DetailThumbnailBatch(
            book_uuid=book.uuid,
            start_index=start_index,
            next_index=end_index,
            has_more=end_index < session.page_count,
            items=tuple(items),
        )

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
        except OSError:
            return None
        return f"{stat.st_mtime_ns}-{stat.st_size}"

    def _session_for(self, book: Book, signature: str) -> ReaderImageSession:
        cache_key = f"session:{book.uuid}:{signature}"
        with self._session_cache_lock:
            session = self._session_cache.get(cache_key)
        if session is not None:
            return session

        # Source scanning is expensive for large books, so keep a source-stable
        # session around for cover and detail thumbnail batches.
        session = self._reader_session_service.open_document(book.file_path)
        with self._session_cache_lock:
            self._session_cache[cache_key] = session
        return session

    def _cover_path(self, book: Book, signature: str, size: SizeTuple) -> Path:
        return self._covers_dir() / f"{self._safe_book_uuid(book.uuid)}-{signature}-{size[0]}x{size[1]}.png"

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
        except OSError:
            return candidates[0]

    def _remove_stale_covers(self, book: Book, keep: Path) -> None:
        safe_uuid = self._safe_book_uuid(book.uuid)
        for path in self._covers_dir().glob(f"{safe_uuid}-*.png"):
            if path != keep:
                path.unlink(missing_ok=True)

    def _cover_cache_key(self, book: Book, signature: str, size: SizeTuple) -> str:
        return f"cover:{book.uuid}:{signature}:{size[0]}x{size[1]}"

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
