"""Optional Library persistence used by a Reader opened from a shelf book.

External documents receive ``None`` and never gain a repository dependency.
The concrete LibraryService implements this structural interface.
"""

from __future__ import annotations

from typing import Protocol

from joyread.core.models.bookmark import Bookmark
from joyread.core.reader.models import ReaderProgress, ReaderSettings


class ReaderLibraryPort(Protocol):
    def get_progress(self, book_uuid: str, book_scope: str = "public") -> ReaderProgress | None: ...

    def set_progress(self, book_uuid: str, page_index: int, progress_percent: float) -> None: ...

    def get_reader_settings(self, book_uuid: str, book_scope: str = "public") -> ReaderSettings | None: ...

    def save_reader_settings(
        self, book_uuid: str, settings: ReaderSettings, book_scope: str = "public"
    ) -> None: ...

    def list_bookmarks(self, book_uuid: str, book_scope: str = "public") -> list[Bookmark]: ...

    def add_bookmark(
        self, book_uuid: str, name: str, page_index: int, book_scope: str = "public"
    ) -> Bookmark: ...

    def rename_bookmark(
        self, book_uuid: str, bookmark_uuid: str, name: str, book_scope: str = "public"
    ) -> None: ...

    def delete_bookmark(
        self, book_uuid: str, bookmark_uuid: str, book_scope: str = "public"
    ) -> None: ...
