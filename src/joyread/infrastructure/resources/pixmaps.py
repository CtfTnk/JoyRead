"""GUI pixmap loading for small, already-generated thumbnail assets.

Archive extraction and page rendering remain in the thumbnail worker service.
The caller keeps the displayed pixmap and only requests a load for a new or
explicitly replaced cover, never for selection or layout updates.
"""
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice
from PySide6.QtGui import QPixmap


def load_thumbnail_pixmap(path: Path) -> QPixmap:
    # Loading bytes bypasses Qt's implicit filename cache. A cover edit may
    # overwrite the same path with equal size/timestamp; it must still refresh.
    pixmap = QPixmap()
    source = QFile(str(path))
    if source.open(QIODevice.OpenModeFlag.ReadOnly):
        pixmap.loadFromData(source.readAll())
        source.close()
    return pixmap
