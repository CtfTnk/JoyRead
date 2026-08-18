"""Bookmark domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Bookmark:
    """User-saved location inside a book.

    ``book_scope`` is ``"public"`` for normal library books and
    ``"private"`` for Hidden Space books — the same page index can exist
    twice with different scopes, so both fields are needed to look it up.
    """

    uuid: str
    book_scope: str
    book_uuid: str
    name: str
    page_index: int
    created_at: datetime
    updated_at: datetime
