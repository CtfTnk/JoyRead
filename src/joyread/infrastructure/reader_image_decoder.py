"""Qt image decoder used by the reader page worker pipeline."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtGui import QImage, QImageReader

from joyread.app.reader_page_pipeline import PreparedReaderPage, ReaderPagePayload, ReaderPageRequest


class QtPageFrameDecoder:
    """Decode and downscale encoded pages before they reach the GUI thread."""

    def decode(
        self,
        payload: ReaderPagePayload,
        request: ReaderPageRequest,
    ) -> PreparedReaderPage[QImage]:
        byte_array = QByteArray(payload.image_bytes)
        buffer = QBuffer(byte_array)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError(f"Could not open page {payload.page_index + 1} for decoding.")
        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            source_size = reader.size()
            source_dimensions = payload.source_dimensions
            if source_size.isValid():
                source_dimensions = (source_size.width(), source_size.height())
            scaled = _fit_size(
                source_dimensions,
                (request.target_width, request.target_height),
            )
            if scaled != source_dimensions:
                reader.setScaledSize(QSize(*scaled))
            image = reader.read()
        finally:
            buffer.close()
        if image.isNull():
            raise RuntimeError(f"Could not decode page {payload.page_index + 1}.")
        image.setDevicePixelRatio(max(1.0, request.device_pixel_ratio))
        return PreparedReaderPage(
            page_index=payload.page_index,
            frame=image.copy(),
            source_dimensions=source_dimensions,
            rendered_dimensions=(image.width(), image.height()),
            generation=request.generation,
        )


def qimage_frame_bytes(value: object) -> int:
    """Return actual QImage backing-store bytes for shared LRU accounting."""

    frame = getattr(value, "frame", value)
    bytes_per_line = getattr(frame, "bytesPerLine", None)
    height = getattr(frame, "height", None)
    if callable(bytes_per_line) and callable(height):
        return max(0, int(bytes_per_line()) * int(height()))
    try:
        return len(value)  # type: ignore[arg-type]
    except TypeError:
        return 0


def _fit_size(source: tuple[int, int], target: tuple[int, int]) -> tuple[int, int]:
    source_width, source_height = source
    target_width, target_height = target
    if source_width <= 0 or source_height <= 0:
        return max(1, target_width), max(1, target_height)
    scale = min(target_width / source_width, target_height / source_height, 1.0)
    return max(1, round(source_width * scale)), max(1, round(source_height * scale))
