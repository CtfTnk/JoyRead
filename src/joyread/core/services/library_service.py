"""Library business service facade."""

from __future__ import annotations

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.repositories.book_repository import BookRepository


class LibraryService:
    def __init__(self, book_repository: BookRepository) -> None:
        self._book_repository = book_repository

    def list_books(self) -> list[Book]:
        return self._book_repository.list_books()

    def list_collections(self) -> list[Collection]:
        return self._book_repository.list_collections()

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
    ) -> None:
        self._book_repository.update_book_metadata(book_uuid, title=title, author=author)

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
