"""Deterministic mock data for the first bookshelf UI phase."""

from __future__ import annotations

from datetime import datetime, timedelta

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.repositories.book_repository import BookRepository


class MockBookRepository(BookRepository):
    def __init__(self) -> None:
        self._now = datetime(2026, 4, 30, 12, 0, 0)
        self._collections = [
            Collection(
                uuid="collection-a",
                name="A Collection",
                is_private=False,
                created_at=self._now - timedelta(days=40),
                updated_at=self._now - timedelta(days=2),
            )
        ]
        self._books = self._build_books()

    def list_books(self) -> list[Book]:
        return list(self._books)

    def list_collections(self) -> list[Collection]:
        return list(self._collections)

    def _build_books(self) -> list[Book]:
        rows = [
            ("Akane-banashi Story 148", "Yuki Suenaga", "Comic", "CBZ", 0.18, True, False),
            ("Spy x Family Vol. 1", "Tatsuya Endo", "PDF", "PDF", 0.42, False, False),
            ("Frieren Beyond Journey", "Kanehito Yamada", "Comic", "CBZ", 0.73, True, False),
            ("Dungeon Meshi Archive", "Ryoko Kui", "Comic", "ZIP", 0.11, False, False),
            ("Mother of All Attacks", "Dachima Inaka", "Novel", "EPUB", 0.33, False, False),
            ("The Apothecary Notes", "Natsu Hyuuga", "Novel", "EPUB", 0.62, True, False),
            ("Blue Period Sketchbook", "Tsubasa Yamaguchi", "Comic", "CBZ", 0.05, False, True),
            ("Witch Hat Atelier", "Kamome Shirahama", "Comic", "CBR", 0.95, True, False),
            ("A Sign of Affection", "Suu Morishita", "Comic", "CBZ", 0.27, False, False),
            ("Ascendance of a Bookworm", "Miya Kazuki", "Novel", "EPUB", 0.49, False, False),
            ("Yotsuba Collection", "Kiyohiko Azuma", "Comic", "7Z", 0.84, True, False),
            ("Local PDF Sample", None, "PDF", "PDF", 0.0, False, True),
            ("Mushishi Volume Notes", "Yuki Urushibara", "Comic", "RAR", 0.58, False, False),
            ("Light Novel Draft", "Mock Author", "Novel", "EPUB", 0.21, False, False),
        ]

        books: list[Book] = []
        for index, (title, author, book_type, file_format, progress, favourite, missing) in enumerate(rows):
            collection_ids = ("collection-a",) if index in {0, 2, 5, 8, 10} else ()
            books.append(
                Book(
                    uuid=f"mock-book-{index + 1:02d}",
                    title=title,
                    author=author,
                    language_tag="English",
                    book_type=book_type,
                    file_format=file_format,
                    file_path=f"/mock/library/{title}.{file_format.lower()}",
                    progress=progress,
                    cover_thumbnail_path=None,
                    added_at=self._now - timedelta(days=index * 3),
                    updated_at=self._now - timedelta(days=index),
                    last_read_at=None if index in {11, 13} else self._now - timedelta(hours=index * 7),
                    is_favourite=favourite,
                    is_missing=missing,
                    collection_ids=collection_ids,
                )
            )
        return books
