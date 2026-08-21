"""Recolour and fade icon SVGs into ready-to-paint pixmaps.

The icon set is drawn in black. Surfaces that need another colour -- white
glyphs on the drag-and-drop scrim, dimmed glyphs on a disabled control -- bake
the change into a pixmap once instead of stacking a ``QGraphicsEffect`` on
every widget that shows one.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap


def faded_pixmap(path: str, size: int, opacity: float) -> QPixmap:
    """Render the SVG at *path* once, at *size*, with *opacity* baked in."""

    return tinted_pixmap(path, size, color=None, opacity=opacity)


def tinted_pixmap(
    path: str,
    size: int,
    *,
    color: QColor | Qt.GlobalColor | str | None = None,
    opacity: float = 1.0,
) -> QPixmap:
    """Render the SVG at *path*, recoloured to *color* and faded to *opacity*.

    ``color`` replaces every drawn pixel while preserving the glyph's alpha --
    ``CompositionMode_SourceIn`` keeps the shape and swaps the ink -- which is
    what the design means by rendering these black icons inverted. Pass ``None``
    to keep the artwork's own colours.
    """

    source = QIcon(path).pixmap(QSize(size, size), _device_pixel_ratio())
    if color is not None:
        source = _recoloured(source, QColor(color))
    if opacity >= 1.0:
        return source

    faded = QPixmap(source.size())
    faded.setDevicePixelRatio(source.devicePixelRatio())
    faded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(faded)
    painter.setOpacity(opacity)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return faded


def _device_pixel_ratio() -> float:
    """Ratio to rasterize SVGs at, so they are not upscaled on a retina screen.

    ``QIcon.pixmap(size)`` without a ratio renders at 1x, which then gets
    stretched -- visible on the drag overlay's 72px icon discs and on every
    display JoyRead's v1.0 target hardware ships with. The application-wide
    ratio is the highest of any attached screen, so a pixmap built here is
    never under-sampled for the screen it ends up on.
    """

    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    return float(app.devicePixelRatio()) or 1.0


def _recoloured(source: QPixmap, color: QColor) -> QPixmap:
    tinted = QPixmap(source.size())
    tinted.setDevicePixelRatio(source.devicePixelRatio())
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    # SourceIn only paints where the destination already has alpha, so a rect
    # that overshoots the logical size on a retina pixmap costs nothing and
    # spares this from having to reason about device pixel ratios.
    painter.fillRect(source.rect(), color)
    painter.end()
    return tinted


__all__ = ["faded_pixmap", "tinted_pixmap"]
