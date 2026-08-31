#!/usr/bin/env python3
"""Render JoyRead's vector file-association icon into a native Windows ICO.

The SVG is the reviewable source of truth.  The ICO deliberately contains
independent renders for every Windows shell size so its document silhouette and
the J badge remain sharp in Explorer rather than relying on a large bitmap
being downscaled at install time.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SVG = ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyReadDocument.svg"
OUTPUT_ICO = ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyReadDocument.ico"
ICON_SIZES = (
    (16, 16),
    (20, 20),
    (24, 24),
    (32, 32),
    (40, 40),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
)


def render_svg_frame(source: Path, size: int) -> Image.Image:
    """Return one transparency-preserving raster frame from ``source``."""

    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG source: {source}")

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()

    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (size, size), rgba.constBits().tobytes())


def build_icon(source: Path = SOURCE_SVG, destination: Path = OUTPUT_ICO) -> Path:
    """Build a native multi-size ICO from the checked-in SVG source."""

    if not source.is_file():
        raise FileNotFoundError(f"Missing document icon source: {source}")

    frames = [render_svg_frame(source, width) for width, _ in ICON_SIZES]
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Pillow uses the base image's dimensions as the maximum ICO frame size.
    # Keep the independent 256 px raster first, then provide the dedicated
    # small renders instead of asking Pillow to downscale it itself.
    frames[-1].save(
        destination,
        format="ICO",
        sizes=ICON_SIZES,
        append_images=frames[:-1],
    )

    with Image.open(destination) as icon:
        sizes = set(icon.info.get("sizes", ()))
        if icon.format != "ICO" or icon.mode != "RGBA" or sizes != set(ICON_SIZES):
            raise RuntimeError(f"Generated a malformed Windows icon: {destination}")
    return destination


def main() -> int:
    destination = build_icon()
    print(f"built {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
