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


class ArchiveResourceLimitError(ArchiveError):
    """A configured archive resource budget was exceeded.

    The structured fields let import and reader surfaces provide a useful,
    localized error without exposing command lines, passwords, or raw backend
    diagnostics.
    """

    def __init__(
        self,
        limit: str,
        *,
        actual: int | None = None,
        maximum: int | None = None,
        subject: str | None = None,
    ) -> None:
        self.limit = limit
        self.actual = actual
        self.maximum = maximum
        self.subject = subject
        # User-facing layers map this structured error to localized text. Keep
        # the exception string generic so an incidental fallback cannot expose
        # archive paths, member names, passwords, or backend diagnostics.
        super().__init__("Archive resource limit exceeded.")
