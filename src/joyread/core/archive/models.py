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
class ArchiveProbeResult:
    """Result of a shallow, non-interactive archive container probe.

    A probe intentionally establishes only that an archive container can be
    listed and advertises at least one supported direct image or nested archive
    candidate.  It does not build a page tree, read image bytes, decode pixels,
    or request a password.  Those operations belong to ``open()`` and the
    reader's controlled error path.
    """

    path: Path
    is_valid: bool
    code: ArchiveValidationCode
    message: str
    archive_format: str | None = None
    is_encrypted: bool = False
    has_direct_images: bool = False
    has_nested_archive_candidates: bool = False
    error_type: str | None = None


# ``validate_archive`` was the public name before probes became deliberately
# lightweight. Keep imports and annotations from integrations source-compatible
# while returning the new shallow result shape.
ArchiveValidationResult = ArchiveProbeResult


@dataclass(frozen=True)
class ArchivePage:
    index: int
    image_bytes: bytes
    dimensions: tuple[int, int]
    display_path: str
