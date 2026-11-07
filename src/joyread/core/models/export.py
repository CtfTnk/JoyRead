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
    #: ``sealed`` means ``storage_path`` is a Hidden Space ``.jrl`` that export
    #: must decrypt; ``stored_hash`` then covers the ciphertext.
    storage_kind: str = "verbatim"
    #: The stored content's format (``CBZ``, ``PDF``...), which names the
    #: exported file's suffix when the stored path's own suffix is ``.jrl``.
    file_format: str = ""
