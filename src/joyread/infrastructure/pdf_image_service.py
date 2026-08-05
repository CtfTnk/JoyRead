"""Qt PDF adapter with viewport-sized worker rendering."""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore, RLock

from PIL import Image, ImageChops
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocument

from joyread.app.reader_page_pipeline import PreparedReaderPage, ReaderPageRequest
from joyread.core.reader.models import ReaderPageImage
from joyread.core.reader.pdf import (
    PDF_EXTENSIONS,
    PdfEmptyError,
    PdfError,
    PdfOpenError,
    PdfPasswordUnsupportedError,
    PdfReadError,
    PdfValidationResult,
)


PDF_RENDER_MAX_LONG_EDGE = 4096
_PDF_FALLBACK_PAGE_SIZE = (612, 792)
_PDF_WHITE_MARGIN_THRESHOLD = 248
_PDF_ALPHA_MARGIN_THRESHOLD = 16
_PDF_CROP_PADDING_RATIO = 0.018
_PDF_MIN_CROP_KEEP_RATIO = 0.55
_PDF_MAX_CROP_KEEP_RATIO = 0.985


class PdfImageSession:
    def __init__(
        self,
        path: Path,
        dimensions: tuple[tuple[int, int], ...],
        *,
        normalize_margins: bool = False,
    ) -> None:
        self._path = path
        self._dimensions = dimensions
        self._normalize_margins = normalize_margins
        self._render_slot = BoundedSemaphore(1)
        self._state_lock = RLock()
        self._closed = False
        self.current_index = 0

    @property
    def page_count(self) -> int:
        return len(self._dimensions)

    @property
    def index_range(self) -> range:
        return range(self.page_count)

    def is_valid_index(self, index: int) -> bool:
        return 0 <= index < self.page_count

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        return self._dimensions[index] if self.is_valid_index(index) else None

    def prepare_page(self, request: ReaderPageRequest, _decoder=None) -> PreparedReaderPage[QImage]:  # noqa: ANN001
        if not self.is_valid_index(request.page_index):
            raise PdfReadError(f"Invalid PDF page {request.page_index + 1}.")
        with self._state_lock:
            if self._closed:
                raise PdfReadError("PDF session is closed.")
        with self._render_slot:
            document = _load_document(self._path)
            try:
                target = _fit_render_size(
                    self._dimensions[request.page_index],
                    (request.target_width, request.target_height),
                )
                image = document.render(request.page_index, QSize(*target))
            finally:
                document.close()
        if image.isNull():
            raise PdfReadError(f"Could not render PDF page {request.page_index + 1}.")
        image.setDevicePixelRatio(max(1.0, request.device_pixel_ratio))
        return PreparedReaderPage(
            page_index=request.page_index,
            frame=image.copy(),
            source_dimensions=self._dimensions[request.page_index],
            rendered_dimensions=(image.width(), image.height()),
            generation=request.generation,
        )

    def prepare_thumbnail_pages(
        self,
        page_indices: Iterable[int],
        size: tuple[int, int],
    ) -> list[PreparedReaderPage[QImage] | None]:
        """Render bounded worker frames without a PNG encode/decode roundtrip."""

        target_width = max(1, int(size[0])) * 2
        target_height = max(1, int(size[1])) * 2
        results: list[PreparedReaderPage[QImage] | None] = []
        for page_index in page_indices:
            if not self.is_valid_index(page_index):
                results.append(None)
                continue
            results.append(
                self.prepare_page(
                    ReaderPageRequest(
                        page_index,
                        target_width,
                        target_height,
                        1.0,
                        0,
                    )
                )
            )
        return results

    def read_page(self, page_index: int):  # noqa: ANN201 - direct preparation is preferred.
        del page_index
        raise PdfReadError("PDF pages must be rendered for a target viewport.")

    def get_page(self, index: int) -> ReaderPageImage | None:
        pages = self.get_pages((index,))
        return pages[0] if pages else None

    def get_pages(self, indices: Iterable[int]) -> list[ReaderPageImage | None]:
        results: list[ReaderPageImage | None] = []
        for index in indices:
            if not self.is_valid_index(index):
                results.append(None)
                continue
            width, height = _fit_render_size(self._dimensions[index], (2048, 2048))
            request = ReaderPageRequest(index, width, height, 1.0, 0)
            prepared = self.prepare_page(request)
            payload, _rendered_dimensions = _qimage_png(prepared.frame)
            dimensions = self._dimensions[index]
            if self._normalize_margins:
                payload, dimensions = _normalize_rendered_pdf_png(payload)
            results.append(ReaderPageImage(index, payload, dimensions))
        return results

    def seek(self, index: int) -> bool:
        if not self.is_valid_index(index):
            return False
        with self._state_lock:
            self.current_index = index
        return True

    def close(self) -> None:
        with self._state_lock:
            self._closed = True


class PdfImageService:
    def __init__(self, *, normalize_margins: bool = False) -> None:
        self._normalize_margins = normalize_margins

    def open(self, path: str | Path) -> PdfImageSession:
        source = Path(path)
        _validate_source(source)
        document = _load_document(source)
        try:
            page_count = document.pageCount()
            if page_count <= 0:
                raise PdfEmptyError(f"No pages found in PDF: {source}")
            dimensions = tuple(_source_dimensions(document, index) for index in range(page_count))
        finally:
            document.close()
        return PdfImageSession(source, dimensions, normalize_margins=self._normalize_margins)

    def validate_pdf(self, path: str | Path) -> PdfValidationResult:
        return self.probe_pdf(path)

    def probe_pdf(self, path: str | Path) -> PdfValidationResult:
        source = Path(path)
        try:
            _validate_source(source)
            document = _load_document(source)
            try:
                page_count = document.pageCount()
            finally:
                document.close()
            if page_count <= 0:
                raise PdfEmptyError(f"No pages found in PDF: {source}")
        except (PdfError, OSError) as exc:
            return PdfValidationResult(False, str(exc), error_type=type(exc).__name__)
        return PdfValidationResult(True, "PDF container contains page content.", page_count)


def _validate_source(source: Path) -> None:
    if not source.exists():
        raise PdfOpenError(f"PDF does not exist: {source}")
    if not source.is_file():
        raise PdfOpenError(f"PDF path is not a file: {source}")
    if source.suffix.lower() not in PDF_EXTENSIONS:
        raise PdfOpenError(f"Unsupported PDF format: {source.suffix or source.name}")


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
    if error in {QPdfDocument.Error.IncorrectPassword, QPdfDocument.Error.UnsupportedSecurityScheme}:
        raise PdfPasswordUnsupportedError(f"Password-protected PDF files are not supported yet: {path}")
    raise PdfOpenError(f"Could not open PDF: {path} ({error.name})")


def _source_dimensions(document: QPdfDocument, page_index: int) -> tuple[int, int]:
    size = document.pagePointSize(page_index)
    if size.width() <= 0 or size.height() <= 0:
        return _PDF_FALLBACK_PAGE_SIZE
    return max(1, round(size.width())), max(1, round(size.height()))


def _fit_render_size(source: tuple[int, int], target: tuple[int, int]) -> tuple[int, int]:
    width, height = source
    target_width, target_height = target
    scale = min(target_width / width, target_height / height)
    rendered = (max(1, round(width * scale)), max(1, round(height * scale)))
    long_edge = max(rendered)
    if long_edge <= PDF_RENDER_MAX_LONG_EDGE:
        return rendered
    clamp = PDF_RENDER_MAX_LONG_EDGE / long_edge
    return max(1, round(rendered[0] * clamp)), max(1, round(rendered[1] * clamp))


def _qimage_png(image: QImage) -> tuple[bytes, tuple[int, int]]:
    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            raise PdfReadError("Could not encode rendered PDF page.")
    finally:
        buffer.close()
    return bytes(byte_array), (image.width(), image.height())


def _normalize_rendered_pdf_png(payload: bytes) -> tuple[bytes, tuple[int, int]]:
    try:
        with Image.open(BytesIO(payload)) as source:
            image = source.convert("RGBA")
    except OSError:
        return payload, _PDF_FALLBACK_PAGE_SIZE
    bbox = _content_bbox(image)
    if bbox is None or not _should_crop(image.size, bbox):
        return payload, image.size
    width, height = image.size
    padding = max(4, round(min(width, height) * _PDF_CROP_PADDING_RATIO))
    left, top, right, bottom = bbox
    cropped = image.crop((max(0, left - padding), max(0, top - padding), min(width, right + padding), min(height, bottom + padding)))
    output = BytesIO()
    cropped.save(output, format="PNG")
    return output.getvalue(), cropped.size


def _content_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    rgba = image.convert("RGBA")
    ink = rgba.convert("L").point(lambda value: 255 if value < _PDF_WHITE_MARGIN_THRESHOLD else 0)
    alpha = rgba.getchannel("A").point(lambda value: 255 if value > _PDF_ALPHA_MARGIN_THRESHOLD else 0)
    return ImageChops.multiply(ink, alpha).getbbox()


def _should_crop(size: tuple[int, int], bbox: tuple[int, int, int, int]) -> bool:
    width, height = size
    left, top, right, bottom = bbox
    width_ratio = max(0, right - left) / width
    height_ratio = max(0, bottom - top) / height
    return (
        width_ratio >= _PDF_MIN_CROP_KEEP_RATIO
        and height_ratio >= _PDF_MIN_CROP_KEEP_RATIO
        and (width_ratio <= _PDF_MAX_CROP_KEEP_RATIO or height_ratio <= _PDF_MAX_CROP_KEEP_RATIO)
    )
