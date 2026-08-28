"""Structured path problems that can be presented without platform parsing in UI code."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PathIssueKind(StrEnum):
    """User-actionable classes of overlong-path failure."""

    WINDOWS_LONG_PATHS_DISABLED = "windows_long_paths_disabled"
    PATH_TOO_LONG_UNSUPPORTED = "path_too_long_unsupported"


@dataclass(frozen=True)
class PathIssue:
    """One diagnosed path failure, stripped of the sensitive full path."""

    kind: PathIssueKind
    operation: str
    path_length: int


class LongPathAccessError(OSError):
    """Raised when a known-overlong path should not be attempted."""

    def __init__(self, issue: PathIssue) -> None:
        self.issue = issue
        super().__init__(
            "Windows long-path support is required for this operation."
            if issue.kind == PathIssueKind.WINDOWS_LONG_PATHS_DISABLED
            else "This operation does not support the requested path length."
        )
