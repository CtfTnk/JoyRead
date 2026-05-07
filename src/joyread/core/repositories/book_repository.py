"""Book repository contracts."""

from __future__ import annotations

from typing import Protocol

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection


class BookRepository(Protocol):
    def list_books(self) -> list[Book]:
        ...

    def list_collections(self) -> list[Collection]:
        ...

    def set_favourite(self, book_id: str, is_favourite: bool) -> None:
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
