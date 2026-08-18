from __future__ import annotations

from io import BytesIO
from pathlib import Path
from threading import Event, Lock, Thread
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from PySide6.QtGui import QPainter, QPdfWriter

from joyread.core.archive import ArchiveImageService
from joyread.core.models.book import Book
from joyread.core.reader import ReaderSessionService
from tests.support.in_memory_book_repository import InMemoryBookRepository
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.core.services.cache_service import BoundedByteCache, CacheService
from joyread.core.services.thumbnail_service import (
    CoverCropState,
    ThumbnailService,
)
from joyread.infrastructure.filesystem.path_service import PathService
from joyread.infrastructure.pdf_image_service import PdfImageService
from joyread.infrastructure.thumbnail_renderer import (
    PillowThumbnailRenderer,
    QtThumbnailRenderer,
    render_contain_blur_thumbnail,
    render_cover_crop,
)


def _thumbnail_service(tmp_path: Path, renderer=None) -> ThumbnailService:  # noqa: ANN001
    paths = PathService(base_dir=tmp_path)
    paths.ensure_directories()
    pool = ArchiveExtractionPool(tmp_path / "pool", max_bytes=64 * 1024 * 1024)
    cache_service = CacheService(
        archive_extraction_pool=pool,
        reader_page_cache_max_bytes=64 * 1024 * 1024,
    )
    archive_service = ArchiveImageService()
    reader_service = ReaderSessionService(archive_service, PdfImageService())
    return ThumbnailService(
        paths,
        archive_service,
        cache_service,
        reader_service,
        thumbnail_renderer=renderer or QtThumbnailRenderer(),
    )


class _RecordingQtThumbnailRenderer:
    def __init__(self) -> None:
        self.delegate = QtThumbnailRenderer()
        self.prepared_sizes: list[tuple[int, int]] = []
        self.encoded_calls = 0

    def render_prepared(self, frame, size):  # noqa: ANN001, ANN201
        self.prepared_sizes.append((frame.width(), frame.height()))
        return self.delegate.render_prepared(frame, size)

    def render_encoded(self, image_bytes, size):  # noqa: ANN001, ANN201
        self.encoded_calls += 1
        return self.delegate.render_encoded(image_bytes, size)

    def render_cover_crop(self, image_bytes, crop_state, size):  # noqa: ANN001, ANN201
        return self.delegate.render_cover_crop(image_bytes, crop_state, size)


class _RegistrySession:
    page_count = 1

    def __init__(self, *, block_reads: bool = False) -> None:
        self.started = Event()
        self.release = Event()
        if not block_reads:
            self.release.set()
        self.close_count = 0
        self.both_started = Event()
        self.max_active_reads = 0
        self._active_reads = 0
        self._active_lock = Lock()

    def get_pages(self, page_indices) -> list[None]:  # noqa: ANN001
        with self._active_lock:
            self._active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self._active_reads)
            if self._active_reads >= 2:
                self.both_started.set()
        self.started.set()
        try:
            assert self.release.wait(timeout=2)
            return [None for _index in page_indices]
        finally:
            with self._active_lock:
                self._active_reads -= 1

    def close(self) -> None:
        self.close_count += 1


class _RegistryReaderService:
    def __init__(self, *, block_reads: bool = False) -> None:
        self.block_reads = block_reads
        self.open_count = 0
        self.sessions: list[_RegistrySession] = []

    def open_document(self, *_args, **_kwargs) -> _RegistrySession:  # noqa: ANN002, ANN003
        self.open_count += 1
        session = _RegistrySession(block_reads=self.block_reads)
        self.sessions.append(session)
        return session


def _registry_thumbnail_service(
    tmp_path: Path,
    reader_service: _RegistryReaderService,
) -> ThumbnailService:
    paths = PathService(base_dir=tmp_path)
    paths.ensure_directories()
    pool = ArchiveExtractionPool(tmp_path / "registry-pool", max_bytes=64 * 1024 * 1024)
    cache_service = CacheService(pool, reader_page_cache_max_bytes=64 * 1024 * 1024)
    return ThumbnailService(
        paths,
        ArchiveImageService(),
        cache_service,
        reader_service,  # type: ignore[arg-type]
        thumbnail_renderer=PillowThumbnailRenderer(),
    )


def _registry_book(tmp_path: Path) -> Book:
    path = tmp_path / "registry.cbz"
    path.write_bytes(b"source")
    return Book(**{**_sample_book().__dict__, "file_path": str(path), "file_id": "registry-file"})


def _sample_book() -> Book:
    return next(book for book in InMemoryBookRepository().list_books() if book.uuid == "mock-book-15")


def _png_bytes(size: tuple[int, int], color: str = "#ffffff") -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _oriented_jpeg_bytes(size: tuple[int, int], orientation: int) -> bytes:
    image = Image.new("RGB", size, "#336699")
    exif = image.getexif()
    exif[274] = orientation
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _write_pdf(path: Path) -> None:
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    painter.drawText(40, 80, "JoyRead PDF")
    painter.end()


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
    unsupported_path = tmp_path / "sample.epub"
    unsupported_path.write_bytes(b"not supported yet")
    unsupported = Book(**{**book.__dict__, "uuid": "unsupported", "file_path": str(unsupported_path)})
    empty_path = tmp_path / "empty.cbz"
    with ZipFile(empty_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("notes.txt", b"no images")
    empty = Book(**{**book.__dict__, "uuid": "empty", "file_path": str(empty_path)})

    assert service.generate_cover(missing, (200, 284)) is None
    assert service.generate_cover(unsupported, (200, 284)) is None
    assert service.generate_cover(empty, (200, 284)) is None


def test_thumbnail_service_uses_cached_cover_when_source_missing(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = _sample_book()
    missing = Book(**{**book.__dict__, "uuid": "missing-cover", "file_path": str(tmp_path / "missing.cbz")})
    covers_dir = service._paths.paths.thumbnails / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    cached = covers_dir / "missing-cover-signature-200x284.png"
    cached.write_bytes(b"cover")

    resolved = service.existing_cover_path(missing, (200, 284))

    assert resolved == cached
    # Second call hits the cover_index cache (no need to re-glob).
    fallback_key = service._cover_fallback_key(missing, (200, 284))
    assert service._cache_service.cover_index.get(fallback_key) == str(cached)
    assert service.existing_cover_path(missing, (200, 284)) == cached
    # When the cached path itself disappears, the next lookup re-globs
    # and either finds another candidate or returns None.
    cached.unlink()
    assert service.existing_cover_path(missing, (200, 284)) is None


def test_thumbnail_service_prefers_explicit_cover_path(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    explicit = tmp_path / "external-cover.png"
    explicit.write_bytes(_png_bytes((10, 20), "#445566"))
    book = Book(**{**_sample_book().__dict__, "cover_thumbnail_path": str(explicit)})

    resolved = service.existing_cover_path(book, (200, 284))

    assert resolved == explicit


def test_thumbnail_service_save_edited_cover_uses_stable_managed_path(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = Book(**{**_sample_book().__dict__, "uuid": "custom cover/book"})
    state = CoverCropState("import:test", 100, 0, 0, (170, 241))

    first = service.save_edited_cover(book, _png_bytes((90, 120), "#aa2244"), state, (170, 241))
    managed_book = Book(**{**book.__dict__, "cover_thumbnail_path": str(first)})
    second = service.save_edited_cover(managed_book, _png_bytes((90, 120), "#22aa44"), state, (170, 241))

    assert first == second
    assert first.name == "custom_cover_book-custom-170x241.png"
    assert first.exists()
    assert service._paths.paths.thumbnails / "covers" in first.parents
    with Image.open(first) as image:
        assert image.size == (170, 241)


def test_cover_crop_fit_center_matches_default_contain_blur_renderer() -> None:
    source = _png_bytes((320, 80), "#cc2222")
    size = (170, 241)
    fill = max(size[0] / 320, size[1] / 80)
    contain = min(size[0] / 320, size[1] / 80)
    state = CoverCropState("page:1", (contain / fill) * 100.0, 0, 0, size)

    assert render_cover_crop(source, state, size) == render_contain_blur_thumbnail(source, size)


def test_thumbnail_service_loads_cover_source_pages_for_archive_and_pdf(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    service = _thumbnail_service(tmp_path)
    archive_book = _sample_book()
    pdf_path = tmp_path / "source.pdf"
    _write_pdf(pdf_path)
    pdf_book = Book(**{**archive_book.__dict__, "uuid": "pdf-source", "file_path": str(pdf_path), "file_format": "PDF"})
    unsupported_path = tmp_path / "source.epub"
    unsupported_path.write_bytes(b"epub placeholder")
    unsupported_book = Book(
        **{**archive_book.__dict__, "uuid": "epub-source", "file_path": str(unsupported_path), "file_format": "EPUB"}
    )

    assert service.load_cover_source_page(archive_book, 0) is not None
    assert service.load_cover_source_page(pdf_book, 0) is not None
    assert service.load_cover_source_page(unsupported_book, 0) is None


def test_thumbnail_source_exposes_only_managed_file_cache_identity(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    legacy_book = _sample_book()
    managed_book = Book(**{**legacy_book.__dict__, "file_id": "content-42"})

    legacy_source = service.open_thumbnail_source(legacy_book)
    managed_source = service.open_thumbnail_source(managed_book)

    assert legacy_source is not None
    assert legacy_source.persistent_cache_key is None
    assert managed_source is not None
    assert managed_source.persistent_cache_key == "file:content-42"
    legacy_source.close()
    managed_source.close()


def test_thumbnail_source_registry_shares_session_until_last_lease_closes(tmp_path: Path) -> None:
    reader_service = _RegistryReaderService()
    service = _registry_thumbnail_service(tmp_path, reader_service)
    book = _registry_book(tmp_path)

    first = service.open_thumbnail_source(book)
    second = service.open_thumbnail_source(book)

    assert first is not None and second is not None
    assert reader_service.open_count == 1
    assert first.source_id == second.source_id
    session = reader_service.sessions[0]

    first.close()
    assert session.close_count == 0
    second.close()
    assert session.close_count == 1
    assert service._session_entries == {}


def test_thumbnail_source_registry_waits_for_active_read_before_close(tmp_path: Path) -> None:
    reader_service = _RegistryReaderService(block_reads=True)
    service = _registry_thumbnail_service(tmp_path, reader_service)
    source = service.open_thumbnail_source(_registry_book(tmp_path))
    assert source is not None
    session = reader_service.sessions[0]

    worker = Thread(target=lambda: source.read_pages((0,)))
    worker.start()
    assert session.started.wait(timeout=2)

    source.close()
    assert session.close_count == 0
    session.release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert session.close_count == 1


def test_thumbnail_source_registry_preserves_backend_read_concurrency(tmp_path: Path) -> None:
    reader_service = _RegistryReaderService(block_reads=True)
    service = _registry_thumbnail_service(tmp_path, reader_service)
    book = _registry_book(tmp_path)
    first = service.open_thumbnail_source(book)
    second = service.open_thumbnail_source(book)
    assert first is not None and second is not None
    session = reader_service.sessions[0]

    workers = [
        Thread(target=lambda source=source: source.read_pages((0,)))
        for source in (first, second)
    ]
    for worker in workers:
        worker.start()

    assert session.both_started.wait(timeout=2)
    first.close()
    second.close()
    assert session.close_count == 0
    session.release.set()
    for worker in workers:
        worker.join(timeout=2)

    assert session.max_active_reads == 2
    assert session.close_count == 1


def test_thumbnail_source_registry_does_not_retain_closed_books(tmp_path: Path) -> None:
    reader_service = _RegistryReaderService()
    service = _registry_thumbnail_service(tmp_path, reader_service)

    for index in range(12):
        path = tmp_path / f"book-{index}.cbz"
        path.write_bytes(b"source")
        book = Book(
            **{
                **_sample_book().__dict__,
                "uuid": f"book-{index}",
                "file_path": str(path),
                "file_id": f"file-{index}",
            }
        )
        source = service.open_thumbnail_source(book)
        assert source is not None
        source.close()

    assert service._session_entries == {}
    assert all(session.close_count == 1 for session in reader_service.sessions)


def test_thumbnail_service_generates_pdf_cover_and_detail_thumbnail(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    service = _thumbnail_service(tmp_path)
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    book = Book(**{**_sample_book().__dict__, "uuid": "pdf", "file_path": str(pdf_path), "file_format": "PDF"})

    cover = service.generate_cover(book, (200, 284))
    detail = service.generate_page_thumbnail(book, 0, (100, 142))

    assert cover is not None
    assert cover.exists()
    assert detail is not None
    with Image.open(cover) as image:
        assert image.size == (200, 284)
    with Image.open(BytesIO(detail)) as image:
        assert image.size == (100, 142)


def test_pdf_thumbnail_uses_target_prepared_frame_without_png_roundtrip(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    renderer = _RecordingQtThumbnailRenderer()
    service = _thumbnail_service(tmp_path, renderer)
    pdf_path = tmp_path / "prepared.pdf"
    _write_pdf(pdf_path)
    book = Book(
        **{
            **_sample_book().__dict__,
            "uuid": "prepared-pdf",
            "file_path": str(pdf_path),
            "file_format": "PDF",
        }
    )

    thumbnail = service.generate_page_thumbnail(book, 0, (100, 142))

    assert thumbnail is not None
    assert renderer.encoded_calls == 0
    assert renderer.prepared_sizes
    assert renderer.prepared_sizes[0][0] <= 200
    assert renderer.prepared_sizes[0][1] <= 284


def test_thumbnail_service_generates_detail_page_thumbnail_bytes(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = _sample_book()

    data = service.generate_page_thumbnail(book, 0, (100, 142))

    assert data is not None
    with Image.open(BytesIO(data)) as image:
        assert image.size == (100, 142)
    assert service.generate_page_thumbnail(book, book.page_count, (100, 142)) is None


def test_thumbnail_service_uses_caller_supplied_detail_cache_for_page_thumbnails(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = _sample_book()
    detail_cache: BoundedByteCache[tuple[int, int, int], bytes] = BoundedByteCache(max_bytes=8 * 1024 * 1024)

    first = service.generate_page_thumbnail(book, 0, (100, 142), detail_cache=detail_cache)
    second = service.generate_page_thumbnail(book, 0, (100, 142), detail_cache=detail_cache)

    assert first is not None
    assert first == second
    # The bytes flow through the caller-supplied cache, not a service-owned one.
    assert detail_cache.current_bytes > 0
    detail_cache.clear()
    assert detail_cache.current_bytes == 0


def test_thumbnail_service_generates_detail_thumbnail_batches_for_large_archive(tmp_path: Path) -> None:
    service = _thumbnail_service(tmp_path)
    book = _sample_book()
    detail_cache: BoundedByteCache[tuple[int, int, int], bytes] = BoundedByteCache(max_bytes=8 * 1024 * 1024)

    first_batch = service.generate_detail_thumbnail_batch(
        book,
        start_index=0,
        batch_size=14,
        size=(100, 142),
        detail_cache=detail_cache,
    )

    assert len(first_batch.items) == 14
    assert first_batch.next_index == 14
    assert first_batch.has_more is True
    assert [item.page_index for item in first_batch.items] == list(range(14))
    with Image.open(BytesIO(first_batch.items[0].image_bytes)) as image:
        assert image.size == (100, 142)
    assert detail_cache.current_bytes > 0

    end_batch = service.generate_detail_thumbnail_batch(
        book,
        start_index=book.page_count,
        batch_size=14,
        size=(100, 142),
        detail_cache=detail_cache,
    )
    assert end_batch.items == ()
    assert end_batch.next_index == book.page_count
    assert end_batch.has_more is False


def test_contain_blur_renderer_outputs_exact_nonblank_sizes() -> None:
    wide = render_contain_blur_thumbnail(_png_bytes((320, 80), "#cc2222"), (100, 142))
    tall = render_contain_blur_thumbnail(_png_bytes((80, 320), "#22cc66"), (100, 142))

    for data in (wide, tall):
        with Image.open(BytesIO(data)) as image:
            assert image.size == (100, 142)
            assert image.convert("RGBA").getbbox() is not None


def test_qt_thumbnail_renderer_prepares_bounded_orientation_aware_preview() -> None:
    renderer = QtThumbnailRenderer()

    prepared = renderer.prepare_preview(
        _oriented_jpeg_bytes((800, 400), 6),
        (180, 310),
        "import:rotated",
    )

    assert prepared.source_token == "import:rotated"
    assert prepared.source_dimensions == (400, 800)
    assert prepared.frame.width() <= 180
    assert prepared.frame.height() <= 310
    assert prepared.frame.height() > prepared.frame.width()
