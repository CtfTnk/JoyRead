"""Hidden Space covers on the shelf: sealed on disk, shown only from memory."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QImage
import pytest

from joyread.core.reader import ReaderSessionService
from joyread.infrastructure.pdf_image_service import PdfImageService
from joyread.core.services.cache_service import CacheService, ThumbnailCacheKey
from joyread.core.services.library_service import LibraryService
from joyread.core.services.thumbnail_service import CoverCropState, ThumbnailService
from joyread.infrastructure.thumbnail_renderer import QtThumbnailRenderer
from tests.support.vault_library import VaultLibrary, build_vault_library, png_bytes, sample_pages, write_cbz

SIZE = (60, 85)


@pytest.fixture
def library(tmp_path: Path):
    lib = build_vault_library(tmp_path)
    yield lib
    lib.close()


@pytest.fixture
def thumbnails(library: VaultLibrary, qapp):  # noqa: ANN001
    cache = CacheService(
        archive_extraction_pool=library.pool,
        reader_page_cache_max_bytes=8 * 1024 * 1024,
        thumbnail_cache_max_bytes=8 * 1024 * 1024,
    )
    sessions = ReaderSessionService(library.archive_service, PdfImageService(), vault_keyring=library.keyring)
    service = ThumbnailService(
        library.paths,
        library.archive_service,
        cache,
        sessions,
        thumbnail_renderer=QtThumbnailRenderer(),
        vault=library.vault,
        sealed_covers=library.cover_store,
    )
    yield service
    service.close()


def _covers_dir(library: VaultLibrary) -> Path:
    return library.paths.paths.thumbnails / "covers"


def _sealed_book(library: VaultLibrary, tmp_path: Path, *, cover: bytes | None):  # noqa: ANN202
    book_id = library.import_file(write_cbz(tmp_path / "incoming" / "Night Notes.cbz", sample_pages(2)))
    if cover is not None:
        generated = _covers_dir(library) / f"{book_id}-generated-200x284.png"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(cover)
    if not library.vault.has_vault:
        library.create_vault()
    library.sealing.hide_books([book_id])
    library.sealing.run_pending()
    assert library.file_row(book_id)["storage_kind"] == "sealed"
    return LibraryService(library.books).get_book(book_id)


def _plaintext_covers(library: VaultLibrary, book_id: str) -> list[Path]:
    return sorted(_covers_dir(library).glob(f"{book_id}*.png")) if _covers_dir(library).is_dir() else []


def test_a_sealed_books_cover_comes_from_memory_never_a_file(
    library: VaultLibrary, thumbnails: ThumbnailService, tmp_path: Path
) -> None:
    cover_png = png_bytes()
    book = _sealed_book(library, tmp_path, cover=cover_png)

    # Nothing hands the UI a path, and nothing writes a generated PNG.
    assert thumbnails.existing_cover_path(book, SIZE) is None
    assert thumbnails.generate_cover(book, SIZE) is None
    assert thumbnails.existing_cover(book, SIZE) is None  # Not decrypted yet.

    cover = thumbnails.load_cover(book, SIZE)

    assert cover is not None and cover.is_in_memory and cover.data == cover_png
    assert thumbnails.existing_cover(book, SIZE) == cover  # Now in memory: no decrypt.
    assert _plaintext_covers(library, book.uuid) == []

    library.vault.lock()
    assert thumbnails.existing_cover(book, SIZE) is None
    assert thumbnails.load_cover(book, SIZE) is None
    assert thumbnails.can_generate_from(book) is False


def test_a_sealed_book_without_a_cover_gets_one_rendered_in_memory(
    library: VaultLibrary, thumbnails: ThumbnailService, tmp_path: Path
) -> None:
    book = _sealed_book(library, tmp_path, cover=None)

    cover = thumbnails.load_cover(book, SIZE)

    assert cover is not None and cover.is_in_memory
    assert not QImage.fromData(cover.data).isNull()
    assert _plaintext_covers(library, book.uuid) == []
    assert not library.cover_store.path_for(book.uuid).exists()  # Memory only.


def test_editing_a_sealed_books_cover_seals_the_result(
    library: VaultLibrary, thumbnails: ThumbnailService, tmp_path: Path
) -> None:
    book = _sealed_book(library, tmp_path, cover=png_bytes())
    crop = CoverCropState(source_id="page-0", zoom_percent=100.0, pan_x=0.0, pan_y=0.0, crop_size=SIZE)

    saved = thumbnails.save_edited_cover(book, sample_pages(1)[0], crop, SIZE)

    assert saved == library.cover_store.path_for(book.uuid)
    assert _plaintext_covers(library, book.uuid) == []
    shown = thumbnails.existing_cover(book, SIZE)
    assert shown is not None and shown.is_in_memory
    assert not saved.read_bytes().startswith(b"\x89PNG")  # Sealed, not an image.


def test_locking_retires_sealed_sessions_and_drops_rendered_thumbnails(
    library: VaultLibrary, thumbnails: ThumbnailService, tmp_path: Path
) -> None:
    book = _sealed_book(library, tmp_path, cover=None)
    source = thumbnails.open_thumbnail_source(book)  # Held open across the lock.
    assert source is not None and source.page_count == 2
    assert source.read_page(0) is not None
    client = thumbnails.issue_thumbnail_cache_client("detail-page")
    key = ThumbnailCacheKey(f"{thumbnails._document_cache_key(book)}:x", 0, SIZE[0], SIZE[1])
    client.put(key, b"decrypted thumbnail")
    assert thumbnails._cache_service.thumbnail_cache.current_bytes > 0

    library.vault.lock()

    assert not thumbnails._session_entries
    assert source._entry.retired and source._entry.closed
    assert source.read_page(0) is None  # Still held, but it reads nothing now.
    assert thumbnails._cache_service.thumbnail_cache.current_bytes == 0
    source.close()
