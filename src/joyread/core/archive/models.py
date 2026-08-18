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


class ArchiveCachePolicy(StrEnum):
    """How one document is allowed to use the shared extraction cache.

    This is the single decision that foreground reading and background warmup
    both obey. Deciding it per caller is how a document ended up refused for
    bulk conversion because it would fill the cache, and then filled the same
    cache anyway through the slower chunked path.
    """

    #: Cheap random access (zip family). The extraction cache is not involved.
    DIRECT = "direct"
    #: Expensive, and the whole document fits: convert it once in the
    #: background and serve every later read from the cache.
    BULK_CONVERT = "bulk_convert"
    #: Expensive, but no backend can convert this container in one pass. Warm
    #: it forward in bounded batches, which is slower but still bounded.
    SEQUENTIAL_WARM = "sequential_warm"
    #: Expensive, and a whole-document cache product must never be built --
    #: it does not fit, or its size cannot be planned. Foreground reads still
    #: work; they just are not persisted.
    ON_DEMAND_ONLY = "on_demand_only"


@dataclass(frozen=True, slots=True)
class ArchiveCachePlan:
    """The cache policy for one document, with the evidence behind it."""

    policy: ArchiveCachePolicy
    reason: str = ""
    declared_page_bytes: int = 0
    has_unknown_page_size: bool = False
    cache_budget_bytes: int = 0

    @property
    def allows_background_warmup(self) -> bool:
        """Whether any background whole-document work may run at all."""

        return self.policy in {
            ArchiveCachePolicy.BULK_CONVERT,
            ArchiveCachePolicy.SEQUENTIAL_WARM,
        }

    @property
    def allows_persistent_page_writes(self) -> bool:
        """Whether foreground reads may persist pages into the shared cache.

        ``ON_DEMAND_ONLY`` says no. Persisting there would grow an unbounded
        partial bundle for a document that can never be completed, which is the
        exact cost the policy exists to avoid. The bounded Reader RAM cache
        still absorbs re-reads, and re-extracting after eviction is accepted.
        """

        return self.policy in {
            ArchiveCachePolicy.BULK_CONVERT,
            ArchiveCachePolicy.SEQUENTIAL_WARM,
        }


class ArchiveConversionStatus(StrEnum):
    """Outcome of one whole-document bulk conversion attempt."""

    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"
    #: No bulk-capable backend for this container or page layout. A bounded
    #: sequential warmup is still allowed.
    UNSUPPORTED = "unsupported"
    #: Policy refuses a whole-document cache product for this document. No
    #: background warming of any kind may run.
    ON_DEMAND_ONLY = "on_demand_only"
    #: The conversion ran and failed. Never retry it through a slower path:
    #: re-reading the same input on demand walks past whatever just stopped it.
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
