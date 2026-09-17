"""DPR-aware cached painting for the shared thumbnail checkerboard."""

from functools import lru_cache
from math import ceil

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap

from joyread.ui.resources.styles.theme import Theme


def thumbnail_placeholder(ratio: float) -> QPixmap:
    return _placeholder(max(1.0, ratio), Theme.detail_thumbnail_width,
                        Theme.detail_thumbnail_height, Theme.detail_thumbnail_radius)


@lru_cache(maxsize=8)
def _placeholder(ratio: float, width: int, height: int, radius: int) -> QPixmap:
    # GUI thread only. Cache the full rounded result, avoiding hundreds of
    # Python-to-Qt fill calls per visible slot on every scroll repaint.
    pixmap = QPixmap(ceil(width * ratio), ceil(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    painter.setClipPath(path)
    colors = (QColor("#d8d8d8"), QColor("#cfcfcf"))
    square = 8
    for y in range(0, height, square):
        for x in range(0, width, square):
            painter.fillRect(x, y, square, square, colors[(x // square + y // square) % 2])
    painter.fillRect(0, 0, width, height, QColor(0, 0, 0, 28))
    painter.end()
    return pixmap
