"""Pure logical-coordinate placement for custom popup menus."""

from PySide6.QtCore import QPoint, QRect, QSize


def popup_position(point: QPoint, size: QSize, bounds: QRect, anchor: QRect | None = None) -> QPoint:
    """Prefer below/right, flip each axis independently, then clamp to bounds."""
    x = point.x() if anchor is None else anchor.x()
    y = point.y() if anchor is None else anchor.y() + anchor.height()
    right = bounds.x() + bounds.width()
    bottom = bounds.y() + bounds.height()
    if x + size.width() > right:
        x = point.x() - size.width() if anchor is None else anchor.x() + anchor.width() - size.width()
    if y + size.height() > bottom:
        y = point.y() - size.height() if anchor is None else anchor.y() - size.height()
    return QPoint(max(bounds.x(), min(x, right - size.width())),
                  max(bounds.y(), min(y, bottom - size.height())))
