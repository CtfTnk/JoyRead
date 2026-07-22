"""Internal data records shared by archive scanning and page sessions."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from joyread.core.archive.errors import ArchiveOpenError


@dataclass(frozen=True)
class ArchiveSource:
    """A top-level or materialized nested archive source."""

    label: str
    suffix: str
    path: Path | None = None
    data: bytes | None = None
    allow_persistent_cache: bool = True

    @property
    def display_name(self) -> str:
        return str(self.path) if self.path is not None else self.label

    def open_arg(self) -> str | BytesIO:
        if self.data is not None:
            return BytesIO(self.data)
        if self.path is None:
            raise ArchiveOpenError(f"Archive source has no path: {self.label}")
        return str(self.path)


@dataclass(frozen=True)
class ArchiveEntry:
    """One backend-listed file, with its declared uncompressed size if known."""

    name: str
    size: int | None
    password: str | None


@dataclass
class PageRecord:
    """A page address retained by a session without retaining image bytes."""

    display_path: str
    source: ArchiveSource
    name: str
    password: str | None
    size: int | None = None
    dimensions: tuple[int, int] | None = None
