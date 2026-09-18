"""Non-interactive reader preview bubble and progress gesture presentation."""
from __future__ import annotations

from math import ceil
from shiboken6 import isValid

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from joyread.core.reader import ReaderDirection
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.i18n.qt_locale import language_events
from joyread.ui.resources.styles.theme import Theme


class ReaderPreviewBubble(QWidget):
    """Paint only this small surface when fading; never capture input or focus."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.opacity = Theme.reader_preview_opacity
        self.index = 0
        self.status = "loading"
        self.pixmap = QPixmap()
        self.anchor = QPoint()
        events = language_events()
        if events is not None:
            events.changed.connect(self.reposition)
        self.hide()

    def set_content(self, index: int, status: str, data: bytes | None) -> None:
        self.index, self.status = index, status
        self.pixmap = QPixmap()
        if data:
            self.pixmap.loadFromData(data)
        self.reposition()

    def _text(self) -> str:
        return t("reader.preview_unavailable") if self.status == "unavailable" else t("reader.loading")

    def reposition(self) -> None:
        font = self.font()
        font.setPixelSize(Theme.thumbnail_index_font_size)
        self.setFont(font)
        metrics = self.fontMetrics()
        self._line = max(Theme.thumbnail_index_min_height, metrics.height())
        content_width = Theme.detail_thumbnail_width if not self.pixmap.isNull() else metrics.horizontalAdvance(self._text())
        content_height = Theme.detail_thumbnail_height if not self.pixmap.isNull() else self._line
        padding = Theme.reader_preview_padding
        width = max(content_width, metrics.horizontalAdvance(str(self.index + 1))) + padding * 2
        height = content_height + Theme.thumbnail_index_gap + self._line + padding * 2 + Theme.reader_preview_arrow_height
        bounds = self.parentWidget().rect().adjusted(Theme.reader_preview_margin, Theme.reader_preview_margin,
                                                   -Theme.reader_preview_margin, -Theme.reader_preview_margin)
        width = min(width, bounds.width())
        x = max(bounds.left(), min(self.anchor.x() - width // 2, bounds.right() - width + 1))
        y = max(bounds.top(), self.anchor.y() - Theme.reader_preview_gap - height)
        self.setGeometry(x, y, width, height)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(self.opacity)
        arrow = Theme.reader_preview_arrow_height
        radius = Theme.reader_preview_radius
        body = QRectF(0.5, 0.5, self.width() - 1, self.height() - arrow - 1)
        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        half = Theme.reader_preview_arrow_width / 2
        tip = max(radius + half, min(self.anchor.x() - self.x(), self.width() - radius - half))
        triangle = QPainterPath()
        triangle.moveTo(tip - half, body.bottom() - 1)
        triangle.lineTo(tip, self.height() - 0.5)
        triangle.lineTo(tip + half, body.bottom() - 1)
        triangle.closeSubpath()
        painter.setBrush(QColor(Theme.color_window))
        painter.setPen(QColor(Theme.color_button_inner_edge))
        painter.drawPath(path.united(triangle))
        padding = Theme.reader_preview_padding
        content = QRectF(padding, padding, self.width() - padding * 2,
                         self.height() - arrow - padding * 2 - self._line - Theme.thumbnail_index_gap)
        if self.pixmap.isNull():
            painter.setPen(QColor(Theme.color_text_muted))
            painter.drawText(content, Qt.AlignmentFlag.AlignCenter, self._text())
        else:
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(content, Theme.detail_thumbnail_radius, Theme.detail_thumbnail_radius)
            painter.setClipPath(clip)
            painter.drawPixmap(content, self.pixmap, QRectF(self.pixmap.rect()))
            painter.restore()
        painter.setPen(QColor(Theme.color_thumbnail_index))
        painter.drawText(QRectF(padding, body.bottom() - padding - self._line, self.width() - padding * 2, self._line),
                         Qt.AlignmentFlag.AlignCenter, str(self.index + 1))


class ReaderPreviewController(QObject):
    """Own timers/coordinates; the ViewModel owns target data and source policy."""

    seek_requested = Signal(int)

    def __init__(self, shell: QWidget, slider, viewmodel) -> None:
        super().__init__(shell)
        self.shell, self.slider, self.viewmodel = shell, slider, viewmodel
        self.window = shell.window()
        self._disposed = False
        self.bubble = ReaderPreviewBubble(shell)
        self.target: int | None = None
        self.dragging = False
        self._hovered = False
        self._position = 0.0
        self._direction = slider.reading_direction
        self._size = (0, 0)
        self.hover = QTimer(self)
        self.hover.setSingleShot(True)
        self.hover.timeout.connect(self._activate)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self._load)
        self.poll = QTimer(self)
        self.poll.setInterval(Theme.reader_preview_poll_ms)
        self.poll.timeout.connect(viewmodel.retry_preview)
        self.fade = QVariantAnimation(self)
        self.fade.setDuration(Theme.reader_preview_fade_ms)
        self.fade.setStartValue(Theme.reader_preview_opacity)
        self.fade.setEndValue(0.0)
        self.fade.valueChanged.connect(self._opacity)
        self.fade.finished.connect(self.close)
        slider.setMouseTracking(True)
        slider.installEventFilter(self)
        shell.installEventFilter(self)
        shell.window().installEventFilter(self)
        viewmodel.preview_changed.connect(self._content)
        # EventHook stores weak bound methods; explicitly disconnect on Qt destruction.
        self.destroyed.connect(lambda: viewmodel.preview_changed.disconnect(self._content))
        shell.destroyed.connect(self._dispose)

    def _dispose(self) -> None:
        self._disposed = True
        QApplication.instance().removeEventFilter(self)
        self.hover.stop()
        self.debounce.stop()
        self.poll.stop()
        self.fade.stop()
        self.viewmodel.preview_changed.disconnect(self._content)
        self.viewmodel.release_preview()

    @property
    def active(self) -> bool:
        return self.dragging or self.hover.isActive() or self.bubble.isVisible()

    def _opacity(self, value: float) -> None:
        self.bubble.opacity = value
        self.bubble.update()

    def _index_at(self, x: float) -> int:
        track = self.slider._track_rect()
        fraction = max(0.0, min(1.0, (x - track.left()) / max(1.0, track.width())))
        if self.slider.reading_direction == ReaderDirection.RIGHT_TO_LEFT:
            fraction = 1.0 - fraction
        return round(self.slider.minimum() + fraction * (self.slider.maximum() - self.slider.minimum()))

    def _activate(self) -> None:
        if self.viewmodel.page_count <= 0 or not self.slider.isVisible():
            return
        self.fade.stop()
        self.bubble.opacity = Theme.reader_preview_opacity
        self.bubble.show()
        self.bubble.raise_()
        self._update_target()

    def _update_target(self) -> None:
        index = self.slider.value() if self.dragging else self._index_at(self._position)
        ratio = self.slider.devicePixelRatioF()
        size = (ceil(Theme.detail_thumbnail_width * ratio), ceil(Theme.detail_thumbnail_height * ratio))
        track = self.slider._track_rect()
        x = self.slider._handle_center_x() if self.dragging else max(track.left(), min(self._position, track.right()))
        self.bubble.anchor = self.slider.mapTo(self.shell, QPoint(round(x), 0))
        self.bubble.reposition()
        if self.target == index and self._size == size:
            return
        self.target, self._size = index, size
        self.poll.stop()
        self.viewmodel.set_preview_target(index, size, load=False)
        self.debounce.stop()
        if self.bubble.status == "loading":
            self.debounce.start(Theme.reader_preview_debounce_ms)

    def _load(self) -> None:
        if self.target is not None and self.bubble.isVisible():
            self.viewmodel.set_preview_target(self.target, self._size, load=True)
            if self.bubble.status == "loading":
                self.poll.start()

    def _content(self, index: int, status: str, data: bytes | None) -> None:
        if status == "closed":
            self.close()
            return
        if index != self.target:
            return
        self.bubble.set_content(index, status, data)
        if status != "loading":
            self.poll.stop()

    def close(self) -> None:
        self.hover.stop()
        self.debounce.stop()
        self.poll.stop()
        self.fade.stop()
        self.bubble.hide()
        self.target = None
        self.viewmodel.release_preview()
        if self.dragging:
            self._end_drag(False)

    def _end_drag(self, commit: bool) -> None:
        index = self.slider.value()
        self.dragging = False
        QApplication.instance().removeEventFilter(self)
        previous = self.slider.blockSignals(True)
        self.slider.setSliderDown(False)
        self.slider.blockSignals(previous)
        if QWidget.mouseGrabber() is self.slider:
            self.slider.releaseMouse()
        self.close()
        if commit:
            self.seek_requested.emit(index)
        else:
            self.slider.setValue(self.viewmodel.current_index)

    def sync_state(self) -> None:
        direction = self.viewmodel.settings.direction
        if direction != self._direction or self.viewmodel.page_count <= 0:
            self.close()
        self._direction = direction

    def eventFilter(self, watched, event) -> bool:
        if self._disposed or not isValid(self.shell):
            return False
        kind = event.type()
        # Consume QWidget input only. Consuming the corresponding QWindow event
        # would bypass Qt's implicit mouse-grab cleanup on macOS.
        if self.dragging and isinstance(watched, QWidget):
            if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self._end_drag(False)
                return True
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
                self._end_drag(False)
                return True
        if watched in (self.shell, self.window):
            if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Close, QEvent.Type.Hide):
                self.close()
            elif kind in (QEvent.Type.Resize, QEvent.Type.Move) and self.bubble.isVisible():
                self._update_target()
        if watched is not self.slider:
            return False
        if kind == QEvent.Type.DevicePixelRatioChange and self.bubble.isVisible():
            self._update_target()
        elif kind == QEvent.Type.Hide:
            self.close()
        elif kind == QEvent.Type.Enter:
            self._hovered = True
            self._position = self.slider.mapFromGlobal(QCursor.pos()).x()
            if self.fade.state() == QVariantAnimation.State.Running:
                self._activate()
            elif not self.dragging:
                self.hover.start(Theme.reader_preview_hover_ms)
        elif kind == QEvent.Type.Leave:
            self._hovered = False
            self.hover.stop()
            if not self.dragging and self.bubble.isVisible():
                self.fade.start()
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if self.viewmodel.page_count <= 0:
                return True
            self.hover.stop()
            self.dragging = True
            self.slider.setFocus(Qt.FocusReason.MouseFocusReason)
            self.slider.setSliderDown(True)
            self.slider.setValue(self._index_at(event.position().x()))
            self.slider.grabMouse()
            QApplication.instance().installEventFilter(self)
            self._activate()
            return True
        elif kind == QEvent.Type.MouseMove:
            self._position = event.position().x()
            if self.dragging:
                self.slider.setValue(self._index_at(self._position))
                self._update_target()
                return True
            if self.bubble.isVisible() and self._hovered:
                self._update_target()
            elif self._hovered and not self.hover.isActive():
                self.hover.start(Theme.reader_preview_hover_ms)
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and self.dragging:
            window = self.shell.window()
            inside = window.rect().contains(window.mapFromGlobal(event.globalPosition().toPoint()))
            self._end_drag(inside)
            return True
        return False
