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
    requires_sequential_warmup: bool = False
    # True when ``path`` is a scratch copy written by the scanner rather than
    # the file the user actually has. It is still a real path -- backends that
    # need one work unchanged -- but it must never be shown or recorded as the
    # archive's identity: a password prompt reading
    # "/var/folders/.../nested-0000.zip" tells the reader nothing.
    spilled: bool = False

    @property
    def display_name(self) -> str:
        if self.path is None or self.spilled:
            return self.label
        return str(self.path)

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


@dataclass(frozen=True)
class ArchiveContainerProbe:
    """Metadata returned by a backend without attempting entry extraction."""

    entries: tuple[ArchiveEntry, ...]
    is_encrypted: bool = False


@dataclass(frozen=True)
class ArchiveListing:
    """Entries plus the backend access policy discovered while listing."""

    entries: tuple[ArchiveEntry, ...]
    requires_sequential_warmup: bool = False


@dataclass(frozen=True)
class PageRecord:
    """A page address retained by a session without retaining image bytes."""

    display_path: str
    source: ArchiveSource
    name: str
    password: str | None
    size: int | None = None
