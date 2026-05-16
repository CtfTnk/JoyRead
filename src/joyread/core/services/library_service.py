"""Library business service facade."""

from __future__ import annotations

from joyread.core.models.book import Book
from joyread.core.models.bookmark import Bookmark
from joyread.core.models.collection import Collection
from joyread.core.models.language import Language
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.reader.models import ReaderProgress, ReaderSettings


class LibraryService:
    def __init__(self, book_repository: BookRepository) -> None:
        self._book_repository = book_repository

    def list_books(self) -> list[Book]:
        return self._book_repository.list_books()

    def get_book(self, book_uuid: str) -> Book | None:
        return self._book_repository.get_book(book_uuid)

    def list_collections(self) -> list[Collection]:
        return self._book_repository.list_collections()

    def list_languages(self) -> list[Language]:
        return self._book_repository.list_languages()

    def set_favourite(self, book_uuid: str, is_favourite: bool) -> None:
        self._book_repository.set_favourite(book_uuid, is_favourite)

    def set_favourites(self, book_uuids: tuple[str, ...], is_favourite: bool) -> None:
        for book_uuid in book_uuids:
            self.set_favourite(book_uuid, is_favourite)

    def update_book_metadata(
        self,
        book_uuid: str,
        *,
        title: str | None = None,
        author: str | None = None,
        language_tag: str | None = None,
    ) -> None:
        self._book_repository.update_book_metadata(
            book_uuid,
            title=title,
            author=author,
            language_tag=language_tag,
        )

    def delete_book(self, book_uuid: str) -> None:
        self._book_repository.delete_book(book_uuid)

    def delete_books(self, book_uuids: tuple[str, ...]) -> None:
        for book_uuid in book_uuids:
            self.delete_book(book_uuid)

    def create_collection(self, name: str) -> Collection:
        return self._book_repository.create_collection(name)

    def rename_collection(self, collection_uuid: str, name: str) -> None:
        self._book_repository.rename_collection(collection_uuid, name)

    def delete_collection(self, collection_uuid: str) -> None:
        self._book_repository.delete_collection(collection_uuid)

    def add_books_to_collection(self, book_uuids: tuple[str, ...], collection_uuid: str) -> None:
        for book_uuid in book_uuids:
            self._book_repository.add_book_to_collection(book_uuid, collection_uuid)

    def remove_books_from_collection(self, book_uuids: tuple[str, ...], collection_uuid: str) -> None:
        for book_uuid in book_uuids:
            self._book_repository.remove_book_from_collection(book_uuid, collection_uuid)

    def remove_books_from_recent(self, book_uuids: tuple[str, ...]) -> None:
        for book_uuid in book_uuids:
            self._book_repository.remove_book_from_recent(book_uuid)

    def get_progress(self, book_uuid: str, book_scope: str = "public") -> ReaderProgress | None:
        return self._book_repository.get_progress(book_uuid, book_scope)

    def set_progress(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        self._book_repository.set_progress(book_uuid, page_index, progress_percent)

    def get_reader_settings(self, book_uuid: str, book_scope: str = "public") -> ReaderSettings | None:
        return self._book_repository.get_reader_settings(book_uuid, book_scope)

    def save_reader_settings(
        self,
        book_uuid: str,
        settings: ReaderSettings,
        book_scope: str = "public",
    ) -> None:
        self._book_repository.save_reader_settings(book_uuid, settings, book_scope)

    def list_bookmarks(self, book_uuid: str, book_scope: str = "public") -> list[Bookmark]:
        return self._book_repository.list_bookmarks(book_uuid, book_scope)

    def add_bookmark(self, book_uuid: str, name: str, page_index: int, book_scope: str = "public") -> Bookmark:
        return self._book_repository.add_bookmark(book_uuid, name, page_index, book_scope)

    def rename_bookmark(
        self,
        book_uuid: str,
        bookmark_uuid: str,
        name: str,
        book_scope: str = "public",
    ) -> None:
        self._book_repository.rename_bookmark(book_uuid, bookmark_uuid, name, book_scope)

    def delete_bookmark(self, book_uuid: str, bookmark_uuid: str, book_scope: str = "public") -> None:
        self._book_repository.delete_bookmark(book_uuid, bookmark_uuid, book_scope)
