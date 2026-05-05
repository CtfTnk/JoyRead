"""Thumbnail and cover generation for archive-backed books."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from joyread.core.archive import ArchiveError, ArchiveImageService
from joyread.core.archive.service import ARCHIVE_EXTENSIONS
from joyread.core.models.book import Book
from joyread.core.services.cache_service import CacheService
from joyread.infrastructure.filesystem.path_service import PathService


SizeTuple = tuple[int, int]


class ThumbnailService:
    """Generates cached cover files and transient detail-page thumbnails."""

    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

    def __init__(
        self,
        paths: PathService,
        archive_service: ArchiveImageService,
        cache_service: CacheService,
    ) -> None:
        self._paths = paths
        self._archive_service = archive_service
        self._cache_service = cache_service

    def can_generate_from(self, book: Book) -> bool:
        source = Path(book.file_path)
        return source.exists() and source.is_file() and source.suffix.lower() in ARCHIVE_EXTENSIONS

    def existing_cover_path(self, book: Book, size: SizeTuple) -> Path | None:
        signature = self._source_signature(book)
        if signature is None:
            return None

        cache_key = self._cover_cache_key(book, signature, size)
        cached = self._cache_service.thumbnail_cache.get(cache_key)
        if cached:
            cached_path = Path(cached)
            if cached_path.exists():
                return cached_path

        cover_path = self._cover_path(book, signature, size)
        if cover_path.exists():
            self._cache_service.thumbnail_cache.put(cache_key, str(cover_path))
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
            session = self._archive_service.open(book.file_path)
            first_page = session.get_image(0)
            if first_page is None:
                return None
            rendered = render_contain_blur_thumbnail(first_page, size)
        except (ArchiveError, OSError, UnidentifiedImageError):
            return None

        cover_path = self._cover_path(book, signature, size)
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(rendered)
        self._remove_stale_covers(book, keep=cover_path)
        self._cache_service.thumbnail_cache.put(self._cover_cache_key(book, signature, size), str(cover_path))
        return cover_path

    def generate_page_thumbnail(self, book: Book, page_index: int, size: SizeTuple) -> bytes | None:
        if page_index < 0 or not self.can_generate_from(book):
            return None

        signature = self._source_signature(book)
        if signature is None:
            return None

        cache_key = self._page_thumbnail_cache_key(book, signature, page_index, size)
        cached = self._cache_service.page_thumbnail_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            session = self._archive_service.open(book.file_path)
            page = session.get_image(page_index)
            if page is None:
                return None
            rendered = render_contain_blur_thumbnail(page, size)
        except (ArchiveError, OSError, UnidentifiedImageError):
            return None

        self._cache_service.page_thumbnail_cache.put(cache_key, rendered)
        return rendered

    def _source_signature(self, book: Book) -> str | None:
        source = Path(book.file_path)
        try:
            stat = source.stat()
        except OSError:
            return None
        return f"{stat.st_mtime_ns}-{stat.st_size}"

    def _cover_path(self, book: Book, signature: str, size: SizeTuple) -> Path:
        return self._covers_dir() / f"{self._safe_book_uuid(book.uuid)}-{signature}-{size[0]}x{size[1]}.png"

    def _covers_dir(self) -> Path:
        return self._paths.paths.thumbnails / "covers"

    def _remove_stale_covers(self, book: Book, keep: Path) -> None:
        safe_uuid = self._safe_book_uuid(book.uuid)
        for path in self._covers_dir().glob(f"{safe_uuid}-*.png"):
            if path != keep:
                path.unlink(missing_ok=True)

    def _cover_cache_key(self, book: Book, signature: str, size: SizeTuple) -> str:
        return f"cover:{book.uuid}:{signature}:{size[0]}x{size[1]}"

    def _page_thumbnail_cache_key(self, book: Book, signature: str, page_index: int, size: SizeTuple) -> str:
        return f"page:{book.uuid}:{signature}:{page_index}:{size[0]}x{size[1]}"

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
