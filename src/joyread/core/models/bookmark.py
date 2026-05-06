"""Bookmark domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Bookmark:
    uuid: str
    book_scope: str
    book_uuid: str
    name: str
    page_index: int
    created_at: datetime
    updated_at: datetime
