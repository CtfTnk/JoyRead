"""Qt-free PDF reader contracts and controlled errors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from joyread.core.file_types import PDF_EXTENSIONS


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


@dataclass(frozen=True)
class PdfValidationResult:
    is_valid: bool
    message: str
    page_count: int | None = None
    error_type: str | None = None


class PdfImageServicePort(Protocol):
    def open(self, path: str | Path): ...  # noqa: ANN201

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
]
