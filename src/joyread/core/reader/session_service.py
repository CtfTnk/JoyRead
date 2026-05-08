"""Reader-facing archive session helpers."""

from __future__ import annotations

from pathlib import Path

from joyread.core.archive import ArchiveImageService, ArchiveImageSession
from joyread.core.archive.models import ArchivePasswordRequest
from joyread.core.reader.models import ReaderPageImage


class ReaderSessionService:
    """Opens archive sessions and loads bounded page payloads for the reader."""

    def __init__(self, archive_image_service: ArchiveImageService) -> None:
        self._archive_image_service = archive_image_service

    def open_archive(self, path: str | Path, password: str | None = None) -> ArchiveImageSession:
        provider = None
        if password is not None:
            provider = lambda _request: password
        return self._archive_image_service.open(path, password_provider=provider)

    def load_page(self, session: ArchiveImageSession, page_index: int) -> ReaderPageImage | None:
        page = session.get_page(page_index)
        if page is None:
            return None
        return ReaderPageImage(page_index=page.index, image_bytes=page.image_bytes, dimensions=page.dimensions)

    def load_pages(self, session: ArchiveImageSession, page_indices: list[int] | tuple[int, ...]) -> dict[int, ReaderPageImage]:
        pages = session.get_pages(page_indices)
        loaded: dict[int, ReaderPageImage] = {}
        for page in pages:
            if page is None:
                continue
            loaded[page.index] = ReaderPageImage(
                page_index=page.index,
                image_bytes=page.image_bytes,
                dimensions=page.dimensions,
            )
        return loaded

    def password_request_label(self, request: ArchivePasswordRequest) -> str:
        return f"{request.archive_format} archive password"
