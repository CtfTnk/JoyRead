"""Reader page canvas that paints loaded archive pages."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import QWidget

from joyread.core.reader import ReaderLayoutResult, ReaderPageImage
from joyread.ui.resources.styles.theme import Theme


class ReaderCanvas(QWidget):
    mouse_moved = QtSignal(QPoint)
    right_clicked = QtSignal()
    left_clicked = QtSignal()
    wheel_scrolled = QtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ReaderCanvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self._layout_result: ReaderLayoutResult | None = None
        self._pixmaps: dict[int, QPixmap] = {}
        self._pan_x = 0.0
        self._status_text = "Loading..."

    def set_layout_result(self, result: ReaderLayoutResult | None, pan_x: float = 0.0) -> None:
        self._layout_result = result
        self._pan_x = pan_x
        self.update()

    def set_page_image(self, image: ReaderPageImage) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(image.image_bytes):
            self._pixmaps[image.page_index] = pixmap
            self.update()

    def set_status_text(self, text: str) -> None:
        self._status_text = text
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self.mouse_moved.emit(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.left_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.pixelDelta().y() or event.angleDelta().y()
        if delta:
            self.wheel_scrolled.emit(delta)
            event.accept()
            return
        super().wheelEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        shell_clip = QPainterPath()
        shell_clip.addRoundedRect(QRectF(self.rect()), Theme.reader_radius, Theme.reader_radius)
        painter.setClipPath(shell_clip)
        painter.fillRect(self.rect(), QColor(Theme.color_reader_background))

        if self._layout_result is None or not self._layout_result.page_draws:
            painter.setPen(QColor(Theme.color_text_muted))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status_text)
            painter.end()
            return

        for draw in self._layout_result.page_draws:
            rect = QRectF(
                draw.rect.x + self._pan_x,
                draw.rect.y,
                draw.rect.width,
                draw.rect.height,
            )
            pixmap = self._pixmaps.get(draw.page_index)
            if pixmap is None or pixmap.isNull():
                _draw_placeholder_page(painter, rect)
            else:
                painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
        painter.end()


def _draw_placeholder_page(painter: QPainter, rect: QRectF) -> None:
    path = QPainterPath()
    path.addRect(rect)
    painter.save()
    painter.setClipPath(path)
    colors = (QColor("#d8d8d8"), QColor("#c8c8c8"))
    square = 28
    left = int(rect.left())
    top = int(rect.top())
    for y in range(top, int(rect.bottom()) + square, square):
        for x in range(left, int(rect.right()) + square, square):
            painter.fillRect(x, y, square, square, colors[((x // square) + (y // square)) % 2])
    painter.fillRect(rect, QColor(0, 0, 0, 36))
    painter.restore()
