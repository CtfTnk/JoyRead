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


class ArchiveConversionStatus(StrEnum):
    """Why a whole-document bulk conversion did or did not publish.

    A single boolean cannot drive the caller correctly. "This container cannot
    be bulk converted" means the chunked path should run instead, while "the
    conversion hit a guardrail" means it must not: re-reading the same input
    through dozens of on-demand extractions walks straight past the limit that
    just fired.
    """

    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"
    #: No bulk-capable backend for this session's container or page layout.
    UNSUPPORTED = "unsupported"
    #: Bulk conversion is capable here but policy declined this document.
    SKIPPED = "skipped"
    #: The conversion ran and failed. Never retry it through a slower path.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ArchiveConversionResult:
    """Outcome of :meth:`ArchiveImageSession.convert_to_cache`."""

    status: ArchiveConversionStatus
    reason: str = ""
    pages: int = 0

    @property
    def is_published(self) -> bool:
        return self.status in {
            ArchiveConversionStatus.PUBLISHED,
            ArchiveConversionStatus.ALREADY_PUBLISHED,
        }

    @property
    def allows_chunked_fallback(self) -> bool:
        """Whether the caller may warm this document the slow way instead.

        Only capability and policy outcomes qualify. ``FAILED`` does not, and
        neither does a raised ``ArchiveResourceLimitError``, ``ArchiveCancelled``
        or ``OSError`` -- those stop the warmup outright.
        """

        return self.status in {
            ArchiveConversionStatus.UNSUPPORTED,
            ArchiveConversionStatus.SKIPPED,
        }


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
