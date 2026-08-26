"""Test-only in-memory book repository fixtures."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from joyread.core.models.book import Book
from joyread.core.models.bookmark import Bookmark
from joyread.core.models.collection import Collection
from joyread.core.models.export import BookExportRecord
from joyread.core.models.language import Language
from joyread.core.reader.models import ReaderProgress, ReaderSettings


class InMemoryBookRepository:
    _LANGUAGES = (
        Language(plain_text="English", iso_code="en"),
        Language(plain_text="Chinese", iso_code="zh"),
        Language(plain_text="Japanese", iso_code="ja"),
        Language(plain_text="Unknown", iso_code="und"),
    )

    def __init__(self, books: list[Book] | None = None, collections: list[Collection] | None = None) -> None:
        self._collections = collections if collections is not None else [
            Collection(
                uuid="collection-a",
                name="A Collection",
                is_private=False,
                created_at=datetime(2026, 3, 21, 12),
                updated_at=datetime(2026, 4, 28, 12),
            )
        ]
        self._books = books if books is not None else _default_books()
        self._progress: dict[tuple[str, str], ReaderProgress] = {}
        self._reader_settings: dict[tuple[str, str], ReaderSettings] = {}
        self._bookmarks: dict[tuple[str, str], list[Bookmark]] = {}

    def list_books(self) -> list[Book]:
        return list(self._books)

    def get_book(self, book_id: str) -> Book | None:
        return next((book for book in self._books if book.uuid == book_id), None)

    def list_collections(self) -> list[Collection]:
        return list(self._collections)

    def list_languages(self) -> list[Language]:
        return list(self._LANGUAGES)

    def get_export_records(self, book_ids: tuple[str, ...]) -> list[BookExportRecord]:
        books_by_id = {book.uuid: book for book in self._books}
        records: list[BookExportRecord] = []
        for book_id in tuple(dict.fromkeys(book_ids)):
            book = books_by_id.get(book_id)
            if book is None:
                continue
            records.append(
                BookExportRecord(
                    book_uuid=book.uuid,
                    title=book.title,
                    storage_path=book.file_path,
                    original_file_name=book.original_file_name or Path(book.file_path).name,
                    hash_algorithm="sha256",
                    stored_hash="",
                    is_missing=book.is_missing,
                )
            )
        return records

    def set_favourite(self, book_id: str, is_favourite: bool) -> None:
        self._books = [book.with_favourite(is_favourite) if book.uuid == book_id else book for book in self._books]

    def set_book_hidden(self, book_id: str, hidden: bool) -> None:
        hidable_ids = {collection.uuid for collection in self._collections if collection.is_hidable}
        next_books: list[Book] = []
        for book in self._books:
            if book.uuid != book_id:
                next_books.append(book)
                continue
            if hidden:
                # Mirror the SQL repo cascade: clear favourite, drop from
                # recent (last_read_at=None), drop non-hidable collections.
                surviving_ids = tuple(value for value in book.collection_ids if value in hidable_ids)
                next_books.append(
                    replace(
                        book,
                        is_hidden=True,
                        is_favourite=False,
                        last_read_at=None,
                        collection_ids=surviving_ids,
                        updated_at=datetime.now(),
                    )
                )
            else:
                next_books.append(replace(book, is_hidden=False, updated_at=datetime.now()))
        self._books = next_books

    def set_collection_hidable(self, collection_id: str, hidable: bool) -> None:
        self._collections = [
            replace(collection, is_hidable=hidable, updated_at=datetime.now())
            if collection.uuid == collection_id
            else collection
            for collection in self._collections
        ]
        if not hidable:
            # Demoting drops hidden books out of this (now-normal) collection.
            self._books = [
                replace(
                    book,
                    collection_ids=tuple(value for value in book.collection_ids if value != collection_id),
                    updated_at=datetime.now(),
                )
                if book.is_hidden and collection_id in book.collection_ids
                else book
                for book in self._books
            ]

    def revert_hidden_state(self) -> None:
        self._books = [
            replace(book, is_hidden=False, updated_at=datetime.now()) if book.is_hidden else book
            for book in self._books
        ]
        self._collections = [
            replace(collection, is_hidable=False, updated_at=datetime.now()) if collection.is_hidable else collection
            for collection in self._collections
        ]

    def list_hidden_book_ids(self) -> list[str]:
        return [book.uuid for book in self._books if book.is_hidden]

    def list_hidable_collection_ids(self) -> list[str]:
        return [collection.uuid for collection in self._collections if collection.is_hidable]

    def update_book_metadata(
        self,
        book_id: str,
        *,
        title: str | None = None,
        author: str | None = None,
        language_tag: str | None = None,
    ) -> None:
        if language_tag is not None and language_tag not in self._language_names_by_code():
            raise ValueError(f"Unknown language code: {language_tag}")
        self._books = [
            replace(
                book,
                title=title if title is not None else book.title,
                author=author if author is not None else book.author,
                language_tag=language_tag if language_tag is not None else book.language_tag,
                language_name=(
                    self._language_names_by_code()[language_tag]
                    if language_tag is not None
                    else book.language_name
                ),
                updated_at=datetime.now(),
            )
            if book.uuid == book_id
            else book
            for book in self._books
        ]

    def set_book_cover_path(self, book_id: str, cover_path: str) -> None:
        self._books = [
            replace(book, cover_thumbnail_path=cover_path, updated_at=datetime.now())
            if book.uuid == book_id
            else book
            for book in self._books
        ]

    def delete_book(self, book_id: str) -> None:
        self._books = [book for book in self._books if book.uuid != book_id]

    def create_collection(self, name: str) -> Collection:
        now = datetime.now()
        collection = Collection(str(uuid4()), name, False, now, now)
        self._collections.append(collection)
        return collection

    def rename_collection(self, collection_id: str, name: str) -> None:
        self._collections = [
            replace(collection, name=name, updated_at=datetime.now()) if collection.uuid == collection_id else collection
            for collection in self._collections
        ]

    def delete_collection(self, collection_id: str) -> None:
        self._collections = [collection for collection in self._collections if collection.uuid != collection_id]
        self._books = [
            replace(
                book,
                collection_ids=tuple(value for value in book.collection_ids if value != collection_id),
                updated_at=datetime.now(),
            )
            if collection_id in book.collection_ids
            else book
            for book in self._books
        ]

    def add_book_to_collection(self, book_id: str, collection_id: str) -> None:
        self._books = [
            replace(book, collection_ids=(*book.collection_ids, collection_id), updated_at=datetime.now())
            if book.uuid == book_id and collection_id not in book.collection_ids
            else book
            for book in self._books
        ]

    def remove_book_from_collection(self, book_id: str, collection_id: str) -> None:
        self._books = [
            replace(
                book,
                collection_ids=tuple(value for value in book.collection_ids if value != collection_id),
                updated_at=datetime.now(),
            )
            if book.uuid == book_id and collection_id in book.collection_ids
            else book
            for book in self._books
        ]

    def remove_book_from_recent(self, book_id: str) -> None:
        self._books = [
            replace(book, last_read_at=None, updated_at=datetime.now())
            if book.uuid == book_id and book.last_read_at is not None
            else book
            for book in self._books
        ]

    def get_progress(self, book_id: str, book_scope: str = "public") -> ReaderProgress | None:
        return self._progress.get((book_scope, book_id))

    def set_progress(self, book_id: str, page_index: int, progress_percent: float) -> None:
        now = datetime.now()
        self._progress[("public", book_id)] = ReaderProgress(page_index, progress_percent)
        self._books = [
            replace(
                book,
                progress=max(0.0, min(100.0, progress_percent)) / 100.0,
                last_read_at=now,
                updated_at=now,
            )
            if book.uuid == book_id
            else book
            for book in self._books
        ]

    def get_reader_settings(self, book_id: str, book_scope: str = "public") -> ReaderSettings | None:
        return self._reader_settings.get((book_scope, book_id))

    def save_reader_settings(
        self,
        book_id: str,
        settings: ReaderSettings,
        book_scope: str = "public",
    ) -> None:
        self._reader_settings[(book_scope, book_id)] = settings

    def add_bookmark(self, book_id: str, name: str, page_index: int, book_scope: str = "public") -> Bookmark:
        now = datetime.now()
        bookmark = Bookmark(str(uuid4()), book_scope, book_id, name, page_index, now, now)
        self._bookmarks.setdefault((book_scope, book_id), []).append(bookmark)
        self._bookmarks[(book_scope, book_id)].sort(key=lambda item: (item.page_index, item.created_at))
        return bookmark

    def list_bookmarks(self, book_id: str, book_scope: str = "public") -> list[Bookmark]:
        return list(self._bookmarks.get((book_scope, book_id), ()))

    def rename_bookmark(
        self,
        book_id: str,
        bookmark_id: str,
        name: str,
        book_scope: str = "public",
    ) -> None:
        key = (book_scope, book_id)
        bookmarks = self._bookmarks.get(key, [])
        for index, bookmark in enumerate(bookmarks):
            if bookmark.uuid == bookmark_id:
                bookmarks[index] = replace(bookmark, name=name, updated_at=datetime.now())
                return
        raise ValueError(f"Bookmark does not exist: {bookmark_id}")

    def delete_bookmark(self, book_id: str, bookmark_id: str, book_scope: str = "public") -> None:
        key = (book_scope, book_id)
        bookmarks = self._bookmarks.get(key, [])
        next_bookmarks = [bookmark for bookmark in bookmarks if bookmark.uuid != bookmark_id]
        if len(next_bookmarks) == len(bookmarks):
            raise ValueError(f"Bookmark does not exist: {bookmark_id}")
        self._bookmarks[key] = next_bookmarks

    @classmethod
    def _language_names_by_code(cls) -> dict[str, str]:
        return {language.iso_code: language.plain_text for language in cls._LANGUAGES}


def _default_books() -> list[Book]:
    rows: list[dict[str, Any]] = [
        _row("mock-book-01", "Akane-banashi Story 148", "Yuki Suenaga", "CBZ", 0.18, True, 18, "2026-04-30T12:00:00", "test_set/Akane-banashi Story 148 (Yuki Suenaga) (z-library.sk, 1lib.sk, z-lib.sk).cbz", ("collection-a",)),
        _row("mock-book-02", "Spy x Family Vol. 1", "Tatsuya Endo", "PDF", 0.42, False, 28, "2026-04-27T12:00:00", "/mock/library/Spy x Family Vol. 1.pdf"),
        _row("mock-book-03", "Frieren Beyond Journey", "Kanehito Yamada", "CBZ", 0.73, True, 35, "2026-04-24T12:00:00", "/mock/library/Frieren Beyond Journey.cbz", ("collection-a",)),
        _row("mock-book-04", "Dungeon Meshi Archive", "Ryoko Kui", "ZIP", 0.11, False, 18, "2026-04-21T12:00:00", "/mock/library/Dungeon Meshi Archive.zip"),
        _row("mock-book-05", "Mother of All Attacks", "Dachima Inaka", "EPUB", 0.33, False, 14, "2026-04-18T12:00:00", "/mock/library/Mother of All Attacks.epub", book_type="Novel"),
        _row("mock-book-06", "The Apothecary Notes", "Natsu Hyuuga", "EPUB", 0.62, True, 24, "2026-04-15T12:00:00", "/mock/library/The Apothecary Notes.epub", ("collection-a",), book_type="Novel"),
        _row("mock-book-07", "Blue Period Sketchbook", "Tsubasa Yamaguchi", "CBZ", 0.05, False, 16, "2026-04-12T12:00:00", "/mock/library/Blue Period Sketchbook.cbz", is_missing=True),
        _row("mock-book-08", "Witch Hat Atelier", "Kamome Shirahama", "CBR", 0.95, True, 42, "2026-04-09T12:00:00", "/mock/library/Witch Hat Atelier.cbr"),
        _row("mock-book-09", "A Sign of Affection", "Suu Morishita", "CBZ", 0.27, False, 20, "2026-04-06T12:00:00", "/mock/library/A Sign of Affection.cbz", ("collection-a",)),
        _row("mock-book-10", "Ascendance of a Bookworm", "Miya Kazuki", "EPUB", 0.49, False, 14, "2026-04-03T12:00:00", "/mock/library/Ascendance of a Bookworm.epub", book_type="Novel"),
        _row("mock-book-11", "Yotsuba Collection", "Kiyohiko Azuma", "7Z", 0.84, True, 32, "2026-03-31T12:00:00", "/mock/library/Yotsuba Collection.7z", ("collection-a",)),
        _row("mock-book-12", "Local PDF Sample", None, "PDF", 0.0, False, 12, "2026-03-28T12:00:00", "/mock/library/Local PDF Sample.pdf", is_missing=True, last_read_at=None),
        _row("mock-book-13", "Mushishi Volume Notes", "Yuki Urushibara", "RAR", 0.58, False, 26, "2026-03-25T12:00:00", "/mock/library/Mushishi Volume Notes.rar"),
        _row("mock-book-14", "Light Novel Draft", "Mock Author", "EPUB", 0.21, False, 14, "2026-03-22T12:00:00", "/mock/library/Light Novel Draft.epub", book_type="Novel", last_read_at=None),
        _row("mock-book-15", "Delicious in Dungeon v14", "Ryoko Kui", "CBZ", 0.0, False, 192, "2026-05-05T12:00:00", "test_set/Delicious in Dungeon v14 (Ryoko Kui) (z-library.sk, 1lib.sk, z-lib.sk).cbz", last_read_at=None),
    ]
    return [_book_from_row(row) for row in rows]


def _row(
    uuid: str,
    title: str,
    author: str | None,
    file_format: str,
    progress: float,
    is_favourite: bool,
    page_count: int,
    added_at: str,
    file_path: str,
    collection_ids: tuple[str, ...] = (),
    *,
    book_type: str = "Comic",
    is_missing: bool = False,
    last_read_at: str | None = "2026-04-30T05:00:00",
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "title": title,
        "author": author,
        "language_tag": "en",
        "book_type": book_type,
        "file_format": file_format,
        "file_path": file_path,
        "progress": progress,
        "cover_thumbnail_path": None,
        "added_at": added_at,
        "updated_at": added_at,
        "last_read_at": last_read_at,
        "is_favourite": is_favourite,
        "is_missing": is_missing,
        "collection_ids": collection_ids,
        "page_count": page_count,
    }


def _book_from_row(row: dict[str, Any]) -> Book:
    file_path = _resolve_test_path(str(row["file_path"]))
    return Book(
        uuid=str(row["uuid"]),
        title=str(row["title"]),
        author=row["author"],
        language_tag=str(row["language_tag"]),
        language_name="English",
        book_type=str(row["book_type"]),
        file_format=str(row["file_format"]),
        file_path=file_path,
        progress=float(row["progress"]),
        cover_thumbnail_path=None,
        added_at=datetime.fromisoformat(str(row["added_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        last_read_at=datetime.fromisoformat(str(row["last_read_at"])) if row["last_read_at"] else None,
        is_favourite=bool(row["is_favourite"]),
        is_missing=bool(row["is_missing"]),
        collection_ids=tuple(row["collection_ids"]),
        page_count=int(row["page_count"]),
        original_file_name=Path(file_path).name,
    )


def _resolve_test_path(path: str) -> str:
    if not path.startswith("test_set/"):
        return path
    candidate = Path(__file__).resolve().parents[2] / path
    if candidate.exists():
        return str(candidate)
    matches = sorted(candidate.parent.glob(f"{candidate.stem.split('(')[0].strip()}*{candidate.suffix}"))
    return str(matches[0] if matches else candidate)
