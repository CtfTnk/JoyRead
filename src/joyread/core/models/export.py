"""Export-facing book file metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookExportRecord:
    book_uuid: str
    title: str
    storage_path: str
    original_file_name: str
    hash_algorithm: str
    stored_hash: str
    is_missing: bool
