"""Target-bounded Qt decoder for thumbnail and cover generation workers."""

from __future__ import annotations

from io import BytesIO
from threading import Event

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageIOHandler, QImageReader

from joyread.app.cover_editor import PreparedCoverSource
from joyread.core.services.thumbnail_service import CoverCropState, SizeTuple, ThumbnailRenderError
from joyread.core.thumbnail_cancellation import check_thumbnail_cancelled


class PillowThumbnailRenderer:
    """Qt-free Pillow renderer used by non-GUI tools and focused tests."""

    def render_encoded(self, image_bytes: bytes, size: SizeTuple, *, cancelled: Event | None = None) -> bytes:
        return render_contain_blur_thumbnail(image_bytes, size, cancelled=cancelled)

    def render_prepared(self, frame: object, size: SizeTuple, *, cancelled: Event | None = None) -> bytes:
        check_thumbnail_cancelled(cancelled)
        if not isinstance(frame, Image.Image):
            raise TypeError("PillowThumbnailRenderer requires a PIL image frame")
        return render_contain_blur_thumbnail_image(frame, size, cancelled=cancelled)

    def render_cover_crop(
        self,
        image_bytes: bytes,
        crop_state: CoverCropState,
        size: SizeTuple,
    ) -> bytes:
        return render_cover_crop(image_bytes, crop_state, size)


class QtThumbnailRenderer:
    """Decode into a small working frame before applying thumbnail styling."""

    _WORKING_SCALE = 2

    def render_encoded(self, image_bytes: bytes, size: SizeTuple, *, cancelled: Event | None = None) -> bytes:
        check_thumbnail_cancelled(cancelled)
        target = _validated_size(size)
        byte_array = QByteArray(image_bytes)
        buffer = QBuffer(byte_array)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise OSError("Could not open thumbnail image bytes")
        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            source_size = reader.size()
            if source_size.isValid():
                scaled = _reader_scaled_size(reader, source_size, _working_box(target))
                if scaled != (source_size.width(), source_size.height()):
                    reader.setScaledSize(QSize(*scaled))
            image = reader.read()
        finally:
            buffer.close()
        check_thumbnail_cancelled(cancelled)
        if image.isNull():
            raise ThumbnailRenderError("Could not decode thumbnail source")
        return self._render_bounded(image, target, cancelled=cancelled)

    def render_prepared(self, frame: object, size: SizeTuple, *, cancelled: Event | None = None) -> bytes:
        check_thumbnail_cancelled(cancelled)
        if not isinstance(frame, QImage) or frame.isNull():
            raise TypeError("QtThumbnailRenderer requires a non-null QImage frame")
        return self._render_bounded(frame, _validated_size(size), cancelled=cancelled)

    def render_cover_crop(
        self,
        image_bytes: bytes,
        crop_state: CoverCropState,
        size: SizeTuple,
    ) -> bytes:
        try:
            return render_cover_crop(image_bytes, crop_state, size)
        except (OSError, UnidentifiedImageError) as exc:
            raise ThumbnailRenderError("Could not render cover crop") from exc

    def prepare_preview(
        self,
        image_bytes: bytes,
        size: SizeTuple,
        source_token: str,
    ) -> PreparedCoverSource[QImage]:
        """Decode an editor preview bounded to the requested physical pixels."""

        target = _validated_size(size)
        byte_array = QByteArray(image_bytes)
        buffer = QBuffer(byte_array)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise OSError("Could not open cover source bytes")
        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            source_size = reader.size()
            source_dimensions = _oriented_dimensions(reader, source_size)
            if source_size.isValid():
                scaled = _reader_scaled_size(reader, source_size, target)
                if scaled != (source_size.width(), source_size.height()):
                    reader.setScaledSize(QSize(*scaled))
            image = reader.read()
        finally:
            buffer.close()
        if image.isNull():
            raise ThumbnailRenderError("Could not decode cover source")
        if source_dimensions == (0, 0):
            source_dimensions = (image.width(), image.height())
        fitted = _fit_within((image.width(), image.height()), target)
        if fitted != (image.width(), image.height()):
            image = image.scaled(
                QSize(*fitted),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        image.setDevicePixelRatio(1.0)
        return PreparedCoverSource(
            source_token=source_token,
            frame=image.copy(),
            source_dimensions=source_dimensions,
        )

    def _render_bounded(self, image: QImage, size: SizeTuple, *, cancelled: Event | None = None) -> bytes:
        check_thumbnail_cancelled(cancelled)
        working_box = _working_box(size)
        fitted = _fit_within((image.width(), image.height()), working_box)
        if fitted != (image.width(), image.height()):
            image = image.scaled(
                QSize(*fitted),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        check_thumbnail_cancelled(cancelled)
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        payload = bytes(rgba.constBits()[: rgba.sizeInBytes()])
        source = Image.frombytes("RGBA", (rgba.width(), rgba.height()), payload)
        return render_contain_blur_thumbnail_image(source, size, cancelled=cancelled)


def render_contain_blur_thumbnail(image_bytes: bytes, size: SizeTuple, *, cancelled: Event | None = None) -> bytes:
    """Render a fixed-size cover with full-page foreground and blurred fill."""

    check_thumbnail_cancelled(cancelled)
    try:
        with Image.open(BytesIO(image_bytes)) as source_image:
            image = ImageOps.exif_transpose(source_image)
            return render_contain_blur_thumbnail_image(image, size, cancelled=cancelled)
    except (OSError, UnidentifiedImageError) as exc:
        raise ThumbnailRenderError("Could not decode thumbnail source") from exc


def render_contain_blur_thumbnail_image(
    source_image: Image.Image, size: SizeTuple, *, cancelled: Event | None = None,
) -> bytes:
    """Render the established thumbnail treatment from a bounded source image."""

    check_thumbnail_cancelled(cancelled)
    width, height = _validated_size(size)
    foreground_source = source_image.convert("RGBA")
    background_source = source_image.convert("RGB")

    background = _resize_to_fill(background_source, size)
    check_thumbnail_cancelled(cancelled)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(8, min(width, height) // 12)))
    check_thumbnail_cancelled(cancelled)
    foreground = _resize_to_contain(foreground_source, size)
    check_thumbnail_cancelled(cancelled)
    x = (width - foreground.width) // 2
    y = (height - foreground.height) // 2
    background = background.convert("RGBA")
    background.alpha_composite(foreground, dest=(x, y))

    check_thumbnail_cancelled(cancelled)
    output = BytesIO()
    background.save(output, format="PNG")
    check_thumbnail_cancelled(cancelled)
    return output.getvalue()


def render_cover_crop(image_bytes: bytes, crop_state: CoverCropState, size: SizeTuple) -> bytes:
    width, height = _validated_size(size)
    try:
        with Image.open(BytesIO(image_bytes)) as source_image:
            image = ImageOps.exif_transpose(source_image)
            foreground_source = image.convert("RGBA")
            background_source = image.convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise ThumbnailRenderError("Could not decode cover source") from exc

    background = _resize_to_fill(background_source, size)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(8, min(width, height) // 12)))
    background = background.convert("RGBA")

    fill_scale = max(width / foreground_source.width, height / foreground_source.height)
    scale = fill_scale * max(1, crop_state.zoom_percent) / 100.0
    resized = foreground_source.resize(
        (
            max(1, round(foreground_source.width * scale)),
            max(1, round(foreground_source.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )
    max_x = max(0.0, (resized.width - width) / 2)
    max_y = max(0.0, (resized.height - height) / 2)
    normalized_x = max(-1.0, min(1.0, float(crop_state.pan_x)))
    normalized_y = max(-1.0, min(1.0, float(crop_state.pan_y)))
    x = ((width - resized.width) // 2) + round(normalized_x * max_x)
    y = ((height - resized.height) // 2) + round(normalized_y * max_y)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay.alpha_composite(resized, dest=(x, y))
    background.alpha_composite(overlay)

    output = BytesIO()
    background.save(output, format="PNG")
    return output.getvalue()


def _working_box(size: SizeTuple) -> SizeTuple:
    return size[0] * QtThumbnailRenderer._WORKING_SCALE, size[1] * QtThumbnailRenderer._WORKING_SCALE


def _validated_size(size: SizeTuple) -> SizeTuple:
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0:
        raise ValueError("Thumbnail size must be positive")
    return width, height


def _fit_within(source: SizeTuple, target: SizeTuple) -> SizeTuple:
    source_width, source_height = source
    target_width, target_height = target
    if source_width <= 0 or source_height <= 0:
        return target
    scale = min(target_width / source_width, target_height / source_height, 1.0)
    return max(1, round(source_width * scale)), max(1, round(source_height * scale))


def _oriented_dimensions(reader: QImageReader, size: QSize) -> SizeTuple:
    if not size.isValid():
        return 0, 0
    transformation = reader.transformation()
    if transformation & QImageIOHandler.Transformation.TransformationRotate90:
        return size.height(), size.width()
    return size.width(), size.height()


def _reader_scaled_size(reader: QImageReader, source_size: QSize, target: SizeTuple) -> SizeTuple:
    oriented = _oriented_dimensions(reader, source_size)
    fitted = _fit_within(oriented, target)
    if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
        return fitted[1], fitted[0]
    return fitted


def _resize_to_fill(image: Image.Image, size: SizeTuple) -> Image.Image:
    width, height = size
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _resize_to_contain(image: Image.Image, size: SizeTuple) -> Image.Image:
    width, height = size
    scale = min(width / image.width, height / image.height)
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
