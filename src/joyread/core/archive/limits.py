"""Archive access limits and bounded-read accounting.

Settings use ``-1`` as their UI sentinel.  The archive core deliberately does
not: every disabled limit is represented as ``None`` here so format backends
cannot accidentally treat a negative byte count as a valid budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from joyread.core.archive.errors import ArchiveResourceLimitError


GIB = 1024**3
MEGAPIXEL = 1_000_000


@dataclass(frozen=True)
class ArchiveOpenLimits:
    """Immutable limits captured when an archive session is opened."""

    nested_archive_max_depth: int | None = 2
    global_file_max_depth: int | None = 100
    max_source_bytes: int | None = 5 * GIB
    max_extracted_item_bytes: int | None = 1 * GIB
    max_operation_bytes: int | None = 4 * GIB
    max_image_pixels: int | None = 400 * MEGAPIXEL
    external_command_timeout_seconds: int | None = 300

    def __post_init__(self) -> None:
        for name in (
            "nested_archive_max_depth",
            "global_file_max_depth",
            "max_source_bytes",
            "max_extracted_item_bytes",
            "max_operation_bytes",
            "max_image_pixels",
            "external_command_timeout_seconds",
        ):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be None or at least 1")

    def cache_signature(self) -> str:
        """Stable policy component for extraction-cache manifests."""

        values = (
            self.nested_archive_max_depth,
            self.global_file_max_depth,
            self.max_source_bytes,
            self.max_extracted_item_bytes,
            self.max_operation_bytes,
            self.max_image_pixels,
            self.external_command_timeout_seconds,
        )
        return ":".join("none" if value is None else str(value) for value in values)


class ArchiveOperationBudget:
    """Counts materialized bytes for one open scan or page-read operation."""

    def __init__(self, maximum: int | None) -> None:
        self._maximum = maximum
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    @property
    def maximum(self) -> int | None:
        return self._maximum

    def consume(self, count: int, subject: str) -> None:
        if count <= 0:
            return
        next_used = self._used + count
        if self._maximum is not None and next_used > self._maximum:
            raise ArchiveResourceLimitError(
                "operation_bytes",
                actual=next_used,
                maximum=self._maximum,
                subject=subject,
            )
        self._used = next_used


def ensure_item_size(size: int | None, maximum: int | None, subject: str) -> None:
    """Reject a known uncompressed item before allocating its payload."""

    if size is not None and maximum is not None and size > maximum:
        raise ArchiveResourceLimitError(
            "extracted_item_bytes",
            actual=size,
            maximum=maximum,
            subject=subject,
        )
