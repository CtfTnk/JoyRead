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

    def delete_book(self, book_uuid: str) -> None:
        self._book_repository.delete_book(book_uuid)

    def delete_books(self, book_uuids: tuple[str, ...]) -> None:
        for book_uuid in book_uuids:
            self.delete_book(book_uuid)
