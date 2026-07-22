"""Public archive model types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ArchivePasswordRequest:
    archive_path: str
    archive_format: str
    attempt: int
    reason: str | None = None


@dataclass(frozen=True)
class ArchivePasswordResponse:
    password: str | None = None
    skip: bool = False


PasswordProvider = Callable[[ArchivePasswordRequest], str | ArchivePasswordResponse | None]


@dataclass(frozen=True)
class ArchiveContentsEntry:
    """One folder-derived table-of-contents target in flattened page order."""

    label: str
    page_index: int
    depth: int = 0


class ArchiveValidationCode(StrEnum):
    OK = "ok"
    MISSING = "missing"
    NOT_FILE = "not_file"
    UNSUPPORTED_FORMAT = "unsupported_format"
    EMPTY = "empty"
    READ_FAILED = "read_failed"
    CORRUPT = "corrupt"
    PASSWORD_REQUIRED = "password_required"
    PASSWORD_REJECTED = "password_rejected"
    DEPENDENCY_MISSING = "dependency_missing"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    OPEN_FAILED = "open_failed"
    UNKNOWN_ERROR = "unknown_error"


class ArchivePasswordPolicy(StrEnum):
    ALLOW = "allow"
    FORBID = "forbid"


class ArchiveAccessMode(StrEnum):
    """Cost/readiness of page access for a discovered archive session."""

    DIRECT = "direct"
    EXPENSIVE_COLD = "expensive_cold"
    EXPENSIVE_READY = "expensive_ready"


@dataclass(frozen=True)
class ArchiveValidationResult:
    """Non-throwing archive check result for import/UI feedback paths."""

    path: Path
    is_valid: bool
    code: ArchiveValidationCode
    message: str
    archive_format: str | None = None
    page_count: int | None = None
    file_size: int | None = None
    mtime_ns: int | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class ArchivePage:
    index: int
    image_bytes: bytes
    dimensions: tuple[int, int]
    display_path: str
