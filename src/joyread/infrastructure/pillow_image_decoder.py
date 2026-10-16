"""Bounded worker-side fallback for image formats absent from a Qt build."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps
from PySide6.QtGui import QColorSpace, QImage


def decode_pillow_image(
    image_bytes: bytes, target: tuple[int, int],
) -> tuple[QImage, tuple[int, int]]:
    """Return an owned, oriented frame within target physical pixels.

    Some Qt wheels carry image plugins whose native codec is unavailable on
    the supported OS (notably Qt 6.8 ARM64 WebP). Pillow already supplies these
    codecs for archive inspection and covers. Keep this fallback in workers,
    and never hand a full-size decoded image or borrowed Python buffer to Qt.
    """

    with Image.open(BytesIO(image_bytes)) as source:
        profile = source.info.get("icc_profile")
        # Avoid an extra full-resolution copy for the common unrotated case.
        ImageOps.exif_transpose(source, in_place=True)
        source_dimensions = source.size
        source.thumbnail(target, Image.Resampling.LANCZOS)
        rgba = source.convert("RGBA")
        pixels = rgba.tobytes()
        frame = QImage(
            pixels, rgba.width, rgba.height, rgba.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
    if profile:
        color_space = QColorSpace.fromIccProfile(profile)
        if color_space.isValid():
            frame.setColorSpace(color_space)
    return frame, source_dimensions
