"""Book repository contracts."""

from __future__ import annotations

from typing import Protocol

from joyread.core.models.book import Book
from joyread.core.models.bookmark import Bookmark
from joyread.core.models.collection import Collection
from joyread.core.models.export import BookExportRecord
from joyread.core.models.language import Language
from joyread.core.reader.models import ReaderProgress, ReaderSettings


class BookRepository(Protocol):
    def list_books(self) -> list[Book]:
        ...

    def list_collections(self) -> list[Collection]:
        ...

    def list_languages(self) -> list[Language]:
        ...

    def get_export_records(self, book_ids: tuple[str, ...]) -> list[BookExportRecord]:
        ...

    def set_favourite(self, book_id: str, is_favourite: bool) -> None:
        ...

    def update_book_metadata(
        self,
        book_id: str,
        *,
        title: str | None = None,
        author: str | None = None,
        language_tag: str | None = None,
    ) -> None:
        ...

    def delete_book(self, book_id: str) -> None:
        ...

    def create_collection(self, name: str) -> Collection:
        ...

    def rename_collection(self, collection_id: str, name: str) -> None:
        ...

    def delete_collection(self, collection_id: str) -> None:
        ...

    def add_book_to_collection(self, book_id: str, collection_id: str) -> None:
        ...

    def get_progress(self, book_id: str, book_scope: str = "public") -> ReaderProgress | None:
        ...

    def set_progress(self, book_id: str, page_index: int, progress_percent: float) -> None:
        ...

    def get_reader_settings(self, book_id: str, book_scope: str = "public") -> ReaderSettings | None:
        ...

    def save_reader_settings(
        self,
        book_id: str,
        settings: ReaderSettings,
        book_scope: str = "public",
    ) -> None:
        ...

    def add_bookmark(self, book_id: str, name: str, page_index: int, book_scope: str = "public") -> Bookmark:
        ...

    def list_bookmarks(self, book_id: str, book_scope: str = "public") -> list[Bookmark]:
        ...

    def rename_bookmark(
        self,
        book_id: str,
        bookmark_id: str,
        name: str,
        book_scope: str = "public",
    ) -> None:
        ...

    def delete_bookmark(self, book_id: str, bookmark_id: str, book_scope: str = "public") -> None:
        ...
