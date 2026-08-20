"""Typed errors raised by JoyRead archive readers."""

from __future__ import annotations


class ArchiveError(Exception):
    """Base class for controlled archive failures."""

    task_failure_kind = "controlled"


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

    task_failure_kind = "expected"

    def __init__(self, message: str, *, archive_path: str | None = None) -> None:
        super().__init__(message)
        self.archive_path = archive_path


class ArchivePasswordRejected(ArchiveError):
    """The provided archive password was rejected."""

    task_failure_kind = "expected"

    def __init__(self, message: str, *, archive_path: str | None = None) -> None:
        super().__init__(message)
        self.archive_path = archive_path


class ArchiveDependencyMissing(ArchiveError):
    """An optional archive backend dependency is unavailable."""


class ArchiveBulkUnsupported(ArchiveError):
    """This container cannot be converted in one whole-document pass.

    A capability statement, not a failure: the caller may still warm the
    document in bounded batches. Backends raise it for inputs they cannot
    express -- a member name a listfile cannot carry, for instance -- so the
    decision stays with the backend that knows its own command surface.
    """

    task_failure_kind = "expected"


class ArchiveCancelled(ArchiveError):
    """A caller cancelled the operation before it finished.

    Distinct from a failure: nothing is wrong with the archive or the backend,
    so the work must not be retried through a different path.
    """

    task_failure_kind = "cancelled"


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
