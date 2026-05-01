"""Book domain model used by the bookshelf UI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class Book:
    uuid: str
    title: str
    author: str | None
    language_tag: str | None
    book_type: str
    file_format: str
    file_path: str
    progress: float
    cover_thumbnail_path: str | None
    added_at: datetime
    updated_at: datetime
    last_read_at: datetime | None
    is_favourite: bool
    is_missing: bool = False
    collection_ids: tuple[str, ...] = ()

    def with_favourite(self, value: bool) -> Book:
        return replace(self, is_favourite=value, updated_at=datetime.now())

    def matches_query(self, query: str) -> bool:
        normalized = query.strip().lower()
        if not normalized:
            return True
        searchable = " ".join(
            part or ""
            for part in (
                self.title,
                self.author,
                self.language_tag,
                self.book_type,
                self.file_format,
            )
        ).lower()
        return normalized in searchable

    @property
    def progress_percent(self) -> int:
        return round(max(0.0, min(1.0, self.progress)) * 100)
