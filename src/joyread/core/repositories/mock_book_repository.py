"""JSON-backed deterministic mock data for the bookshelf UI."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.repositories.book_repository import BookRepository


class MockBookRepository(BookRepository):
    """Read bundled mock records from package data instead of hardcoded rows."""

    _DATA_PACKAGE = "joyread.core.repositories"
    _DATA_FILE = "mock_library.json"
    _TEST_SET_PREFIX = "test_set/"

    def __init__(self, data_path: Path | None = None) -> None:
        raw_data = self._load_json(data_path)
        self._collections = [self._build_collection(row) for row in raw_data.get("collections", [])]
        self._books = [self._build_book(row) for row in raw_data.get("books", [])]

    def list_books(self) -> list[Book]:
        return list(self._books)

    def list_collections(self) -> list[Collection]:
        return list(self._collections)

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
        book_data["file_path"] = self._resolve_mock_path(str(book_data["file_path"]))
        book_data["added_at"] = self._parse_datetime(str(book_data["added_at"]))
        book_data["updated_at"] = self._parse_datetime(str(book_data["updated_at"]))
        book_data["last_read_at"] = self._parse_optional_datetime(book_data.get("last_read_at"))
        book_data["collection_ids"] = tuple(book_data.get("collection_ids") or ())
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
