"""Reader-facing document session helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from joyread.core.archive import ArchiveImageService, ArchiveImageSession
from joyread.core.archive.service import ARCHIVE_EXTENSIONS, EXPENSIVE_ARCHIVE_EXTENSIONS
from joyread.core.archive.models import ArchivePasswordRequest, ArchivePasswordResponse
from joyread.core.reader.epub_session import EPUB_EXTENSIONS, EpubReaderSession, open_epub_session
from joyread.core.reader.models import ReaderPageImage
from joyread.core.reader.pdf_session import PDF_EXTENSIONS, PdfImageService


logger = logging.getLogger(__name__)


# EPUB is text-flow, not image-paged; the reader-window code path
# branches on suffix before consulting this union, so EPUB never
# reaches the ``ReaderImageSession`` Protocol below.
SUPPORTED_READER_EXTENSIONS = ARCHIVE_EXTENSIONS | PDF_EXTENSIONS | EPUB_EXTENSIONS


class ReaderImageSession(Protocol):
    page_count: int

    def get_page(self, index: int):  # noqa: ANN201 - archive and PDF page models share attributes.
        ...

    def get_pages(self, indices):  # noqa: ANN001, ANN201 - accepts any iterable of page indexes.
        ...

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        ...


class ReaderSessionService:
    """Opens supported reader documents and loads bounded page payloads."""

    def __init__(
        self,
        archive_image_service: ArchiveImageService,
        pdf_image_service: PdfImageService | None = None,
    ) -> None:
        self._archive_image_service = archive_image_service
        self._pdf_image_service = pdf_image_service or PdfImageService()

    def open_document(
        self,
        path: str | Path,
        password: str | None = None,
        passwords: dict[str, str] | None = None,
        skipped_archives: set[str] | None = None,
        *,
        archive_internal_max_depth: int = 2,
    ) -> ReaderImageSession:
        suffix = Path(path).suffix.lower()
        logger.info("Opening reader document: path=%s suffix=%s", path, suffix)
        if suffix in ARCHIVE_EXTENSIONS:
            return self.open_archive(
                path,
                password=password,
                passwords=passwords,
                skipped_archives=skipped_archives,
                archive_internal_max_depth=archive_internal_max_depth,
            )
        if suffix in PDF_EXTENSIONS:
            return self._pdf_image_service.open(path)
        raise ValueError(f"Unsupported reader format: {suffix or Path(path).name}")

    def open_epub(self, path: str | Path) -> EpubReaderSession:
        """Open an EPUB and return a chapter-flow session.

        Lives alongside ``open_archive`` / ``open_document`` so callers
        (the novel viewmodel, tests) can address it directly without
        going through the image-paged routing in ``open_document``.
        """
        return open_epub_session(path)

    def open_archive(
        self,
        path: str | Path,
        password: str | None = None,
        passwords: dict[str, str] | None = None,
        skipped_archives: set[str] | None = None,
        *,
        archive_internal_max_depth: int = 2,
    ) -> ArchiveImageSession:
        provider = None
        password_map = dict(passwords or {})
        skipped = set(skipped_archives or ())
        if password is not None:
            password_map.setdefault(str(Path(path)), password)
        if password_map or skipped:
            # Adapt the reader's simple "password dict + skipped set" inputs
            # to the full ``PasswordProvider`` protocol the archive service
            # expects. The reader VM never touches the protocol type
            # directly — keeping that translation here so callers stay
            # ignorant of the archive-service API shape.
            provider = lambda request: (
                ArchivePasswordResponse(skip=True)
                if request.archive_path in skipped
                else password_map.get(request.archive_path)
            )
        return self._archive_image_service.open(
            path,
            password_provider=provider,
            max_depth=archive_internal_max_depth,
        )

    def load_page(self, session: ReaderImageSession, page_index: int) -> ReaderPageImage | None:
        page = session.get_page(page_index)
        if page is None:
            return None
        loaded_index = getattr(page, "index", getattr(page, "page_index", None))
        if loaded_index is None:
            return None
        return ReaderPageImage(page_index=int(loaded_index), image_bytes=page.image_bytes, dimensions=page.dimensions)

    def load_pages(
        self,
        session: ReaderImageSession,
        page_indices: list[int] | tuple[int, ...],
    ) -> dict[int, ReaderPageImage]:
        pages = session.get_pages(page_indices)
        loaded: dict[int, ReaderPageImage] = {}
        for page in pages:
            if page is None:
                continue
            page_index = getattr(page, "index", getattr(page, "page_index", None))
            if page_index is None:
                continue
            loaded[int(page_index)] = ReaderPageImage(
                page_index=int(page_index),
                image_bytes=page.image_bytes,
                dimensions=page.dimensions,
            )
        return loaded

    def should_warm_disk_cache(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in EXPENSIVE_ARCHIVE_EXTENSIONS

    def warm_disk_cache(
        self,
        path: str | Path,
        *,
        password: str | None = None,
        archive_internal_max_depth: int = 2,
        chunk_size: int = 8,
        is_cancelled=None,  # noqa: ANN001 - accepts TaskHandle-like status checks.
    ) -> None:
        """Warm extracted-page disk cache in descending page order.

        The reader keeps its own foreground session. Warm-up opens a separate
        archive session so slow whole-book extraction never holds the visible
        reader's session lock or blocks current/nearby page loads.
        """

        if not self.should_warm_disk_cache(path):
            return
        logger.debug("Warming disk cache for %s", path)
        session = self.open_archive(
            path,
            password=password,
            archive_internal_max_depth=archive_internal_max_depth,
        )
        page_indices = list(range(session.page_count - 1, -1, -1))
        chunk_size = max(1, int(chunk_size))
        for start in range(0, len(page_indices), chunk_size):
            if is_cancelled is not None and is_cancelled():
                logger.debug("Disk cache warm cancelled at chunk start=%d", start)
                return
            self.load_pages(session, tuple(page_indices[start : start + chunk_size]))
        logger.debug("Disk cache warm complete for %s", path)

    def password_request_label(self, request: ArchivePasswordRequest) -> str:
        return f"{request.archive_format} archive password"
