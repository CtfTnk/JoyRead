"""PDF-backed image page sessions for the reader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtPdf import QPdfDocument

from joyread.core.reader.models import ReaderPageImage


PDF_EXTENSIONS = frozenset({".pdf"})
PDF_RENDER_DPI = 144
PDF_RENDER_MAX_LONG_EDGE = 4096
_PDF_POINTS_PER_INCH = 72.0
_PDF_FALLBACK_PAGE_SIZE = (1224, 1584)


class PdfError(Exception):
    """Base error for controlled PDF reader failures."""


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


class PdfImageSession:
    """Bounded access to PDF pages rendered as images."""

    def __init__(self, path: Path, dimensions: tuple[tuple[int, int], ...]) -> None:
        self._path = path
        self._dimensions = dimensions
        self.current_index = 0

    @property
    def page_count(self) -> int:
        return len(self._dimensions)

    @property
    def index_range(self) -> range:
        return range(0, self.page_count)

    def is_valid_index(self, index: int) -> bool:
        return 0 <= index < self.page_count

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        if not self.is_valid_index(index):
            return None
        return self._dimensions[index]

    def get_page(self, index: int) -> ReaderPageImage | None:
        return self.get_pages((index,))[0]

    def get_pages(self, indices: Iterable[int]) -> list[ReaderPageImage | None]:
        requested = list(indices)
        results: list[ReaderPageImage | None] = [None] * len(requested)
        valid = [(result_index, page_index) for result_index, page_index in enumerate(requested) if self.is_valid_index(page_index)]
        if not valid:
            return results

        document = _load_document(self._path)
        try:
            for result_index, page_index in valid:
                image_bytes, dimensions = _render_page(document, page_index, self._dimensions[page_index])
                results[result_index] = ReaderPageImage(page_index, image_bytes, dimensions)
        finally:
            document.close()
        return results

    def get_image(self, index: int) -> bytes | None:
        page = self.get_page(index)
        if page is None:
            return None
        return page.image_bytes

    def current(self) -> bytes | None:
        return self.get_image(self.current_index)

    def seek(self, index: int) -> bool:
        if not self.is_valid_index(index):
            return False
        self.current_index = index
        return True


class PdfImageService:
    """Open and validate PDF files as rendered image page sources."""

    def open(self, path: str | Path) -> PdfImageSession:
        source = Path(path)
        if not source.exists():
            raise PdfOpenError(f"PDF does not exist: {source}")
        if not source.is_file():
            raise PdfOpenError(f"PDF path is not a file: {source}")
        if source.suffix.lower() not in PDF_EXTENSIONS:
            raise PdfOpenError(f"Unsupported PDF format: {source.suffix or source.name}")

        document = _load_document(source)
        try:
            page_count = document.pageCount()
            if page_count <= 0:
                raise PdfEmptyError(f"No pages found in PDF: {source}")
            dimensions = tuple(_target_dimensions(document, index) for index in range(page_count))
        finally:
            document.close()
        return PdfImageSession(source, dimensions)

    def validate_pdf(self, path: str | Path) -> PdfValidationResult:
        source = Path(path)
        try:
            session = self.open(source)
            first_page = session.get_page(0)
        except PdfError as exc:
            return PdfValidationResult(False, str(exc), error_type=type(exc).__name__)
        except OSError as exc:
            return PdfValidationResult(False, str(exc), error_type=type(exc).__name__)
        if first_page is None:
            return PdfValidationResult(
                False,
                f"PDF pages were listed but the first page could not be rendered: {source}",
                page_count=session.page_count,
                error_type=PdfReadError.__name__,
            )
        return PdfValidationResult(True, f"PDF is readable with {session.page_count} page(s).", session.page_count)


def _load_document(path: Path) -> QPdfDocument:
    document = QPdfDocument()
    error = document.load(str(path))
    if error == QPdfDocument.Error.None_:
        return document
    document.close()
    if error == QPdfDocument.Error.FileNotFound:
        raise PdfOpenError(f"PDF does not exist: {path}")
    if error == QPdfDocument.Error.InvalidFileFormat:
        raise PdfOpenError(f"Invalid PDF file: {path}")
    if error in {
        QPdfDocument.Error.IncorrectPassword,
        QPdfDocument.Error.UnsupportedSecurityScheme,
    }:
        raise PdfPasswordUnsupportedError(f"Password-protected PDF files are not supported yet: {path}")
    raise PdfOpenError(f"Could not open PDF: {path} ({error.name})")


def _target_dimensions(document: QPdfDocument, page_index: int) -> tuple[int, int]:
    page_size = document.pagePointSize(page_index)
    width = float(page_size.width())
    height = float(page_size.height())
    if width <= 0 or height <= 0:
        return _PDF_FALLBACK_PAGE_SIZE

    scale = PDF_RENDER_DPI / _PDF_POINTS_PER_INCH
    pixel_width = max(1, round(width * scale))
    pixel_height = max(1, round(height * scale))
    long_edge = max(pixel_width, pixel_height)
    if long_edge > PDF_RENDER_MAX_LONG_EDGE:
        clamp = PDF_RENDER_MAX_LONG_EDGE / long_edge
        pixel_width = max(1, round(pixel_width * clamp))
        pixel_height = max(1, round(pixel_height * clamp))
    return pixel_width, pixel_height


def _render_page(document: QPdfDocument, page_index: int, dimensions: tuple[int, int]) -> tuple[bytes, tuple[int, int]]:
    width, height = dimensions
    image = document.render(page_index, QSize(width, height))
    if image.isNull():
        raise PdfReadError(f"Could not render PDF page {page_index + 1}.")

    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            raise PdfReadError(f"Could not encode PDF page {page_index + 1}.")
    finally:
        buffer.close()
    return bytes(byte_array), (int(image.width()), int(image.height()))
