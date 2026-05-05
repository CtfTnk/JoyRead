from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from joyread.core.archive import ArchiveImageService
from joyread.core.models.book import Book
from joyread.core.repositories.mock_book_repository import MockBookRepository
from joyread.core.services.cache_service import CacheService
from joyread.core.services.thumbnail_service import ThumbnailService, render_contain_blur_thumbnail
from joyread.infrastructure.filesystem.path_service import PathService


def _thumbnail_service(tmp_path: Path) -> ThumbnailService:
    paths = PathService(base_dir=tmp_path)
    paths.ensure_directories()
    return ThumbnailService(paths, ArchiveImageService(), CacheService(thumbnail_limit_mb=128, page_limit_mb=512))


def _sample_book() -> Book:
    return next(book for book in MockBookRepository().list_books() if book.uuid == "mock-book-15")


def _png_bytes(size: tuple[int, int], color: str = "#ffffff") -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_thumbnail_service_generates_and_reuses_cover(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = _sample_book()

    first = service.generate_cover(book, (200, 284))
    second = service.generate_cover(book, (200, 284))

    assert first is not None
    assert first == second
    assert first.exists()
    assert tmp_path in first.parents
    with Image.open(first) as image:
        assert image.size == (200, 284)


def test_thumbnail_service_returns_none_for_missing_unsupported_and_empty_archives(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = _sample_book()
    missing = Book(**{**book.__dict__, "uuid": "missing", "file_path": str(tmp_path / "missing.cbz")})
    unsupported_path = tmp_path / "sample.pdf"
    unsupported_path.write_bytes(b"%PDF")
    unsupported = Book(**{**book.__dict__, "uuid": "unsupported", "file_path": str(unsupported_path)})
    empty_path = tmp_path / "empty.cbz"
    with ZipFile(empty_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("notes.txt", b"no images")
    empty = Book(**{**book.__dict__, "uuid": "empty", "file_path": str(empty_path)})

    assert service.generate_cover(missing, (200, 284)) is None
    assert service.generate_cover(unsupported, (200, 284)) is None
    assert service.generate_cover(empty, (200, 284)) is None


def test_thumbnail_service_generates_detail_page_thumbnail_bytes(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = _sample_book()

    data = service.generate_page_thumbnail(book, 0, (100, 142))

    assert data is not None
    with Image.open(BytesIO(data)) as image:
        assert image.size == (100, 142)
    assert service.generate_page_thumbnail(book, book.page_count, (100, 142)) is None


def test_contain_blur_renderer_outputs_exact_nonblank_sizes() -> None:
    wide = render_contain_blur_thumbnail(_png_bytes((320, 80), "#cc2222"), (100, 142))
    tall = render_contain_blur_thumbnail(_png_bytes((80, 320), "#22cc66"), (100, 142))

    for data in (wide, tall):
        with Image.open(BytesIO(data)) as image:
            assert image.size == (100, 142)
            assert image.convert("RGBA").getbbox() is not None
