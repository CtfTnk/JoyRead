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


PasswordProvider = Callable[[ArchivePasswordRequest], str | None]


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
    OPEN_FAILED = "open_failed"
    UNKNOWN_ERROR = "unknown_error"


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
