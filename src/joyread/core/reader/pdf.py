"""Qt-free PDF reader contracts and controlled errors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from joyread.core.file_types import PDF_EXTENSIONS
from joyread.core.vault import SealedFile, VaultLockedError


class PdfError(Exception):
    """Base error for controlled PDF reader failures."""

    task_failure_kind = "controlled"


class PdfOpenError(PdfError):
    pass


class PdfPasswordUnsupportedError(PdfError):
    pass


class PdfEmptyError(PdfError):
    pass


class PdfReadError(PdfError):
    pass


class PdfVaultLocked(PdfError, VaultLockedError):
    """A sealed PDF was read while Hidden Space is locked.

    The PDF twin of ``ArchiveVaultLocked``: a controlled PDF failure that a
    reader can still recognise as "locked" by catching ``VaultLockedError``.
    """

    task_failure_kind = "expected"

    def __init__(self, message: str = "Hidden Space is locked.") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class PdfValidationResult:
    is_valid: bool
    message: str
    page_count: int | None = None
    error_type: str | None = None


class PdfImageServicePort(Protocol):
    def open(self, path: str | Path): ...  # noqa: ANN201

    def open_sealed(self, sealed: SealedFile, *, source_path: Path): ...  # noqa: ANN201

    def probe_pdf(self, path: str | Path) -> PdfValidationResult: ...

    def validate_pdf(self, path: str | Path) -> PdfValidationResult: ...


__all__ = [
    "PDF_EXTENSIONS",
    "PdfEmptyError",
    "PdfError",
    "PdfImageServicePort",
    "PdfOpenError",
    "PdfPasswordUnsupportedError",
    "PdfReadError",
    "PdfValidationResult",
    "PdfVaultLocked",
]
