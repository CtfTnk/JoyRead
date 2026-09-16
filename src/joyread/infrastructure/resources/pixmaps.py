"""GUI pixmap loading and preparation of cached presentation snapshots.

Archive extraction and page rendering remain in the thumbnail worker service.
The caller keeps the displayed pixmap and only requests a load for a new or
explicitly replaced cover, never for selection or layout updates.
"""
from pathlib import Path

from PIL import Image, ImageFilter

from PySide6.QtCore import QFile, QIODevice, Qt
from PySide6.QtGui import QImage, QPixmap


def load_thumbnail_pixmap(path: Path) -> QPixmap:
    # Loading bytes bypasses Qt's implicit filename cache. A cover edit may
    # overwrite the same path with equal size/timestamp; it must still refresh.
    pixmap = QPixmap()
    source = QFile(str(path))
    if source.open(QIODevice.OpenModeFlag.ReadOnly):
        pixmap.loadFromData(source.readAll())
        source.close()
    return pixmap


# Work on a smaller image; callers cache finished snapshots rather than
# filtering during painting. Radius is adjusted for device pixels below.
_BLUR_SHRINK = 4


def downsample_for_blur(source: QPixmap) -> QImage:
    """Prepare a small RGBA source reusable across cached blur states."""

    small = source.scaled(
        max(1, source.width() // _BLUR_SHRINK),
        max(1, source.height() // _BLUR_SHRINK),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return small.toImage().convertToFormat(QImage.Format.Format_RGBA8888)


def blurred_pixmap(source: QPixmap, radius: float, sampled: QImage | None = None) -> QPixmap:
    """Pillow Gaussian approximation on a small image, in logical-pixel radii.

    The snapshot is immutable. Callers retain the result for the gesture's
    lifetime; painting draws or blends cached pixmaps without filtering again.
    """

    if radius <= 0 or source.isNull():
        return source
    if sampled is None:
        sampled = downsample_for_blur(source)
    payload = bytes(sampled.constBits()[: sampled.sizeInBytes()])
    pillow_source = Image.frombytes(
        "RGBA", (sampled.width(), sampled.height()), payload
    )
    device_radius = radius * (source.devicePixelRatio() or 1.0)
    payload = pillow_source.filter(
        ImageFilter.GaussianBlur(device_radius / _BLUR_SHRINK)
    ).tobytes()
    blurred = QPixmap.fromImage(
        QImage(
            payload,
            sampled.width(),
            sampled.height(),
            sampled.width() * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
    )
    while blurred.width() < source.width() or blurred.height() < source.height():
        # Reconstruct the cached image with successive smooth resampling steps.
        blurred = blurred.scaled(
            min(blurred.width() * 2, source.width()),
            min(blurred.height() * 2, source.height()),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    blurred.setDevicePixelRatio(source.devicePixelRatio())
    return blurred
