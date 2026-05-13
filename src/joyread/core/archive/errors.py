"""Typed errors raised by JoyRead archive readers."""

from __future__ import annotations


class ArchiveError(Exception):
    """Base class for controlled archive failures."""


class ArchiveUnsupportedFormat(ArchiveError):
    """The file suffix is not a supported comic archive format."""


class ArchiveOpenError(ArchiveError):
    """The archive could not be opened from the provided path."""


class ArchiveCorruptError(ArchiveError):
    """The archive structure is invalid or unreadable."""


class ArchiveEmptyError(ArchiveError):
    """The archive opened but no supported image pages were found."""


class ArchiveReadError(ArchiveError):
    """A listed archive entry could not be extracted or decoded as an image."""


class ArchivePasswordRequired(ArchiveError):
    """The archive is encrypted and no usable password was provided."""

    def __init__(self, message: str, *, archive_path: str | None = None) -> None:
        super().__init__(message)
        self.archive_path = archive_path


class ArchivePasswordRejected(ArchiveError):
    """The provided archive password was rejected."""

    def __init__(self, message: str, *, archive_path: str | None = None) -> None:
        super().__init__(message)
        self.archive_path = archive_path


class ArchiveDependencyMissing(ArchiveError):
    """An optional archive backend dependency is unavailable."""
