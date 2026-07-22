"""Format-specific archive readers behind the ArchiveImageService facade."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.records import ArchiveEntry, ArchiveSource
from joyread.core.archive.scanner import ArchiveScanContext

from joyread.core.archive.formats.rar_backend import RarArchiveBackend
from joyread.core.archive.formats.seven_zip_backend import SevenZipArchiveBackend
from joyread.core.archive.formats.zip_backend import ZipArchiveBackend


class ArchiveFormatBackend(Protocol):
    """Uniform list/read contract used by the archive facade and scanner."""

    def list_entries(self, source: ArchiveSource, context: ArchiveScanContext) -> list[ArchiveEntry]: ...

    def read_entries(
        self,
        source: ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes]: ...


__all__ = [
    "ArchiveFormatBackend",
    "RarArchiveBackend",
    "SevenZipArchiveBackend",
    "ZipArchiveBackend",
]
