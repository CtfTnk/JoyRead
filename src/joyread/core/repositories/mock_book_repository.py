"""JSON-backed deterministic mock data for the bookshelf UI."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.models.export import BookExportRecord
from joyread.core.models.language import Language
from joyread.core.reader.models import ReaderProgress, ReaderSettings
from joyread.core.repositories.book_repository import BookRepository


class MockBookRepository(BookRepository):
    """Read bundled mock records from package data instead of hardcoded rows."""

    _DATA_PACKAGE = "joyread.core.repositories"
    _DATA_FILE = "mock_library.json"
    _TEST_SET_PREFIX = "test_set/"
    _LANGUAGES = (
        Language(plain_text="English", iso_code="en"),
        Language(plain_text="Chinese", iso_code="zh"),
        Language(plain_text="Japanese", iso_code="ja"),
        Language(plain_text="Unknown", iso_code="und"),
    )

    def __init__(self, data_path: Path | None = None) -> None:
        raw_data = self._load_json(data_path)
        self._collections = [self._build_collection(row) for row in raw_data.get("collections", [])]
        self._books = [self._build_book(row) for row in raw_data.get("books", [])]
        self._progress: dict[tuple[str, str], ReaderProgress] = {}
        self._reader_settings: dict[tuple[str, str], ReaderSettings] = {}

    def list_books(self) -> list[Book]:
        return list(self._books)

    def list_collections(self) -> list[Collection]:
        return list(self._collections)

    def list_languages(self) -> list[Language]:
        return list(self._LANGUAGES)

    def get_export_records(self, book_ids: tuple[str, ...]) -> list[BookExportRecord]:
        target_ids = tuple(dict.fromkeys(book_ids))
        books_by_id = {book.uuid: book for book in self._books}
        return [
            self._export_record_for_book(book)
            for book_id in target_ids
            if (book := books_by_id.get(book_id)) is not None
        ]

    def _export_record_for_book(self, book: Book) -> BookExportRecord:
        original_file_name = (
            book.original_file_name
            or _basename(book.file_path)
            or f"{book.title}.{book.file_format.lower()}"
        )
        return BookExportRecord(
            book_uuid=book.uuid,
            title=book.title,
            storage_path=book.file_path,
            original_file_name=original_file_name,
            hash_algorithm="sha256",
            content_hash="",
            is_missing=book.is_missing,
        )

    def set_favourite(self, book_id: str, is_favourite: bool) -> None:
        self._books = [
            book.with_favourite(is_favourite) if book.uuid == book_id else book
            for book in self._books
        ]

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
        now = datetime.now()
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
                updated_at=now,
            )
            if book.uuid == book_id
            else book
            for book in self._books
        ]

    def delete_book(self, book_id: str) -> None:
        self._books = [book for book in self._books if book.uuid != book_id]

    def create_collection(self, name: str) -> Collection:
        now = datetime.now()
        collection = Collection(
            uuid=str(uuid4()),
            name=name,
            is_private=False,
            created_at=now,
            updated_at=now,
        )
        self._collections.append(collection)
        return collection

    def rename_collection(self, collection_id: str, name: str) -> None:
        now = datetime.now()
        self._collections = [
            replace(collection, name=name, updated_at=now) if collection.uuid == collection_id else collection
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
            replace(
                book,
                collection_ids=(*book.collection_ids, collection_id),
                updated_at=datetime.now(),
            )
            if book.uuid == book_id and collection_id not in book.collection_ids
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

    def _load_json(self, data_path: Path | None) -> dict[str, Any]:
        if data_path is not None:
            return json.loads(data_path.read_text(encoding="utf-8"))

        data_resource = resources.files(self._DATA_PACKAGE).joinpath(self._DATA_FILE)
        return json.loads(data_resource.read_text(encoding="utf-8"))

    def _build_collection(self, row: dict[str, Any]) -> Collection:
        return Collection(
            uuid=row["uuid"],
            name=row["name"],
            is_private=bool(row["is_private"]),
            created_at=self._parse_datetime(row["created_at"]),
            updated_at=self._parse_datetime(row["updated_at"]),
        )

    def _build_book(self, row: dict[str, Any]) -> Book:
        valid_fields = {field.name for field in fields(Book)}
        book_data = {key: value for key, value in row.items() if key in valid_fields}
        file_path = str(book_data["file_path"])
        book_data["file_path"] = self._resolve_mock_path(file_path)
        book_data["original_file_name"] = book_data.get("original_file_name") or _basename(file_path)
        book_data["added_at"] = self._parse_datetime(str(book_data["added_at"]))
        book_data["updated_at"] = self._parse_datetime(str(book_data["updated_at"]))
        book_data["last_read_at"] = self._parse_optional_datetime(book_data.get("last_read_at"))
        book_data["collection_ids"] = tuple(book_data.get("collection_ids") or ())
        book_data["language_tag"] = self._normalize_language_tag(book_data.get("language_tag"))
        book_data["language_name"] = self._language_names_by_code()[book_data["language_tag"]]
        return Book(**book_data)

    def _resolve_mock_path(self, file_path: str) -> str:
        if not file_path.startswith(self._TEST_SET_PREFIX):
            return file_path

        # Mock library rows may reference dev-only pressure-test books. Keep
        # those files outside the package tree so they never ship as app data.
        relative_path = Path(file_path)
        candidates = (
            Path(__file__).resolve().parents[4] / relative_path,
            Path.cwd() / relative_path,
        )
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return str(candidates[0])

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @staticmethod
    def _parse_optional_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(str(value))

    @classmethod
    def _language_names_by_code(cls) -> dict[str, str]:
        return {language.iso_code: language.plain_text for language in cls._LANGUAGES}

    @classmethod
    def _language_codes_by_name(cls) -> dict[str, str]:
        return {language.plain_text.lower(): language.iso_code for language in cls._LANGUAGES}

    @classmethod
    def _normalize_language_tag(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if normalized in cls._language_names_by_code():
            return normalized
        return cls._language_codes_by_name().get(normalized.lower(), "und")


def _basename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]
