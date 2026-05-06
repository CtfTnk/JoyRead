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

    def delete_book(self, book_id: str) -> None:
        ...
