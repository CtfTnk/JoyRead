"""PDF-backed image page sessions for the reader."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtPdf import QPdfDocument
from PIL import Image, ImageChops

from joyread.core.reader.models import ReaderPageImage


logger = logging.getLogger(__name__)


PDF_EXTENSIONS = frozenset({".pdf"})
PDF_RENDER_DPI = 144
PDF_RENDER_MAX_LONG_EDGE = 4096
_PDF_POINTS_PER_INCH = 72.0
_PDF_FALLBACK_PAGE_SIZE = (1224, 1584)
_PDF_WHITE_MARGIN_THRESHOLD = 248
_PDF_ALPHA_MARGIN_THRESHOLD = 16
_PDF_CROP_PADDING_RATIO = 0.018
_PDF_MIN_CROP_KEEP_RATIO = 0.55
_PDF_MAX_CROP_KEEP_RATIO = 0.985


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

    def __init__(
        self,
        path: Path,
        dimensions: tuple[tuple[int, int], ...],
        *,
        normalize_margins: bool = False,
    ) -> None:
        self._path = path
        self._dimensions = list(dimensions)
        self._normalize_margins = normalize_margins
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
                image_bytes, dimensions = _render_page(
                    document,
                    page_index,
                    self._dimensions[page_index],
                    normalize_margins=self._normalize_margins,
                )
                self._dimensions[page_index] = dimensions
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

    def __init__(self, *, normalize_margins: bool = False) -> None:
        # Automatic PDF margin cropping can change correctly authored page
        # boxes. Keep reader output faithful by default; tests and future UI
        # settings can opt in for damaged scans.
        self._normalize_margins = normalize_margins

    def open(self, path: str | Path) -> PdfImageSession:
        source = Path(path)
        logger.debug("PDF open: path=%s", source)
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
        logger.info("PDF opened: %s pages=%d", source.name, page_count)
        return PdfImageSession(source, dimensions, normalize_margins=self._normalize_margins)

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


def _render_page(
    document: QPdfDocument,
    page_index: int,
    dimensions: tuple[int, int],
    *,
    normalize_margins: bool = False,
) -> tuple[bytes, tuple[int, int]]:
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
    payload = bytes(byte_array)
    if normalize_margins:
        return _normalize_rendered_pdf_png(payload)
    return payload, (image.width(), image.height())


def _normalize_rendered_pdf_png(payload: bytes) -> tuple[bytes, tuple[int, int]]:
    """Trim obvious PDF page-box margins without risking content loss."""

    try:
        with Image.open(BytesIO(payload)) as source:
            image = source.convert("RGBA")
    except OSError:
        return payload, _png_dimensions(payload)

    bbox = _content_bbox(image)
    if bbox is None or not _should_crop(image.size, bbox):
        return payload, image.size

    width, height = image.size
    padding = max(4, round(min(width, height) * _PDF_CROP_PADDING_RATIO))
    left, top, right, bottom = bbox
    crop_box = (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )
    if crop_box == (0, 0, width, height):
        return payload, image.size

    cropped = image.crop(crop_box)
    output = BytesIO()
    cropped.save(output, format="PNG")
    return output.getvalue(), cropped.size


def _content_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    rgba = image.convert("RGBA")
    gray = rgba.convert("L")
    alpha = rgba.getchannel("A")
    ink_mask = gray.point(lambda value: 255 if value < _PDF_WHITE_MARGIN_THRESHOLD else 0)
    alpha_mask = alpha.point(lambda value: 255 if value > _PDF_ALPHA_MARGIN_THRESHOLD else 0)
    return ImageChops.multiply(ink_mask, alpha_mask).getbbox()


def _should_crop(size: tuple[int, int], bbox: tuple[int, int, int, int]) -> bool:
    width, height = size
    left, top, right, bottom = bbox
    content_width = max(0, right - left)
    content_height = max(0, bottom - top)
    if content_width <= 0 or content_height <= 0:
        return False
    width_ratio = content_width / width
    height_ratio = content_height / height
    if width_ratio < _PDF_MIN_CROP_KEEP_RATIO or height_ratio < _PDF_MIN_CROP_KEEP_RATIO:
        return False
    if width_ratio > _PDF_MAX_CROP_KEEP_RATIO and height_ratio > _PDF_MAX_CROP_KEEP_RATIO:
        return False
    return True


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(payload)) as image:
            return image.size
    except OSError:
        return _PDF_FALLBACK_PAGE_SIZE
