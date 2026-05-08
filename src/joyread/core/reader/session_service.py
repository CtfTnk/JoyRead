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
        image_bytes = session.get_image(page_index)
        dimensions = session.get_dimensions(page_index)
        if image_bytes is None or dimensions is None:
            return None
        return ReaderPageImage(page_index=page_index, image_bytes=image_bytes, dimensions=dimensions)

    def password_request_label(self, request: ArchivePasswordRequest) -> str:
        return f"{request.archive_format} archive password"
