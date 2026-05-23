"""Book domain model used by the bookshelf UI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from joyread.core.search import build_book_search_document, matches_book_search, parse_book_search_query


# Placeholder page count used when a row is constructed before the reader has
# inspected the file. The actual page count is filled in lazily once the
# reader session opens; 14 matches the detail panel thumbnail grid size, so
# placeholder cards render in a complete grid instead of a half-empty one.
DEFAULT_PAGE_COUNT_PLACEHOLDER = 14


@dataclass(frozen=True)
class Book:
    """Single library entry as the shelf and reader see it.

    Frozen so it is safe to share between the UI thread and worker threads
    without locks; mutations go through :func:`dataclasses.replace`.
    ``file_path`` is the storage path inside JoyRead's managed ``Books/``
    directory; ``original_file_name`` is preserved for export so users can
    recover the name they imported with.
    """

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
    is_hidden: bool = False
    collection_ids: tuple[str, ...] = ()
    page_count: int = DEFAULT_PAGE_COUNT_PLACEHOLDER
    language_name: str | None = None
    original_file_name: str | None = None

    def with_favourite(self, value: bool) -> Book:
        return replace(self, is_favourite=value, updated_at=datetime.now())

    def matches_query(self, query: str) -> bool:
        return matches_book_search(
            build_book_search_document(self.uuid, self.title, self.author),
            parse_book_search_query(query),
        )

    @property
    def progress_percent(self) -> int:
        return round(max(0.0, min(1.0, self.progress)) * 100)
