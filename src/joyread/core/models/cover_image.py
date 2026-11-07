"""A shelf cover: a PNG file on disk, or PNG bytes that exist only in memory.

A normal book's cover is a file under ``Thumbnails/covers``, and the shelf
widgets load it by path. A Hidden Space book's cover is sealed on disk, so the
shelf receives its decrypted PNG bytes instead, which must never be written
back out as a file. One value type carries either, so the ViewModel, the shelf
and the detail panel treat both kinds of cover the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CoverImage:
    """Exactly one of ``path`` or ``data`` is set."""

    path: Path | None = None
    # Excluded from repr: a decrypted cover is a picture of a hidden book, and
    # a repr can end up in a log line or a test failure message.
    data: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.path is None) == (self.data is None):
            raise ValueError("A cover image is either a file path or in-memory bytes.")

    @classmethod
    def from_path(cls, path: Path | str) -> CoverImage:
        return cls(path=Path(path))

    @classmethod
    def from_bytes(cls, data: bytes) -> CoverImage:
        return cls(data=bytes(data))

    @property
    def is_in_memory(self) -> bool:
        """Decrypted Hidden Space material, to forget when the vault locks."""

        return self.data is not None


__all__ = ["CoverImage"]
