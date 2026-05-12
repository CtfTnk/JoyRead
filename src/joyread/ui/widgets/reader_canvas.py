"""Reader page canvas that paints loaded archive pages."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from joyread.core.reader import ReaderLayoutResult, ReaderPageImage
from joyread.ui.resources.styles.theme import Theme


# Spinner geometry expressed as a fraction of the placeholder rect's shorter
# edge so the indicator scales naturally with page size. The label sits below
# the spinner with a small visual gap.
_SPINNER_SIZE_RATIO = 0.18
_SPINNER_MAX_SIZE = 56
_SPINNER_MIN_SIZE = 24
_SPINNER_THICKNESS_RATIO = 0.12
_SPINNER_GAP_TO_LABEL = 12
_SPINNER_ARC_SWEEP = 110  # degrees of the moving arc
_SPINNER_TICK_INTERVAL_MS = 70  # ~14 frames/sec — smooth enough, cheap to run
_SPINNER_PHASE_STEP_DEG = 28


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
        # Spinner phase advances while any visible page is still loading. The
        # timer is created lazily and stopped as soon as every draw has its
        # pixmap, so a fully-loaded spread costs nothing.
        self._spinner_phase = 0.0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(_SPINNER_TICK_INTERVAL_MS)
        self._spinner_timer.timeout.connect(self._tick_spinner)

    def set_layout_result(self, result: ReaderLayoutResult | None, pan_x: float = 0.0) -> None:
        self._layout_result = result
        self._pan_x = pan_x
        self._prune_pixmaps_to_layout()
        self._refresh_spinner_state()
        self.update()

    def set_page_image(self, image: ReaderPageImage) -> None:
        if self._layout_result is not None and not self._layout_draws_page(image.page_index):
            return
        existing = self._pixmaps.get(image.page_index)
        if existing is not None and not existing.isNull():
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(image.image_bytes):
            self._pixmaps[image.page_index] = pixmap
            self._refresh_spinner_state()
            self.update()

    def clear_pages(self) -> None:
        self._layout_result = None
        self._pixmaps.clear()
        self._pan_x = 0.0
        if self._spinner_timer.isActive():
            self._spinner_timer.stop()
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
                _draw_placeholder_page(painter, rect, self._spinner_phase)
            else:
                painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
        painter.end()

    def _refresh_spinner_state(self) -> None:
        """Start or stop the spinner timer based on visible-page coverage.

        Animating only when at least one drawn page is unloaded keeps the
        canvas idle (no timer wakeups) once a spread is fully painted. The
        timer is owned by the widget so it is stopped automatically when the
        canvas is destroyed.
        """

        if self._has_missing_pixmaps():
            if not self._spinner_timer.isActive():
                self._spinner_timer.start()
        elif self._spinner_timer.isActive():
            self._spinner_timer.stop()

    def _has_missing_pixmaps(self) -> bool:
        if self._layout_result is None:
            return False
        for draw in self._layout_result.page_draws:
            pixmap = self._pixmaps.get(draw.page_index)
            if pixmap is None or pixmap.isNull():
                return True
        return False

    def _layout_draws_page(self, page_index: int) -> bool:
        return self._layout_result is not None and any(
            draw.page_index == page_index for draw in self._layout_result.page_draws
        )

    def _prune_pixmaps_to_layout(self) -> None:
        if self._layout_result is None:
            self._pixmaps.clear()
            return
        visible = {draw.page_index for draw in self._layout_result.page_draws}
        for page_index in tuple(self._pixmaps):
            if page_index not in visible:
                self._pixmaps.pop(page_index, None)

    def _tick_spinner(self) -> None:
        # Phase advances monotonically; ``%`` keeps the value small so the
        # float doesn't lose precision over very long sessions.
        self._spinner_phase = (self._spinner_phase + _SPINNER_PHASE_STEP_DEG) % 360.0
        self.update()


def _draw_placeholder_page(painter: QPainter, rect: QRectF, spinner_phase: float) -> None:
    """Draw the checkerboard placeholder and overlay a loading indicator.

    The indicator is intentionally drawn for the *visible* page rect (rather
    than the canvas as a whole) so that in a DOUBLE spread the user sees the
    spinner exactly over whichever page is still loading.
    """

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
    _draw_loading_indicator(painter, rect, spinner_phase)
    painter.restore()


def _draw_loading_indicator(painter: QPainter, rect: QRectF, phase_deg: float) -> None:
    """Centered spinner + label so loading state is unmistakable.

    Skipped when the page rect is too small to host a legible spinner (e.g.
    far-off prefetch pages in vertical mode) — drawing a tiny illegible
    indicator is more distracting than just showing the placeholder.
    """

    shorter = min(rect.width(), rect.height())
    if shorter < _SPINNER_MIN_SIZE * 2:
        return

    diameter = max(_SPINNER_MIN_SIZE, min(_SPINNER_MAX_SIZE, int(shorter * _SPINNER_SIZE_RATIO)))
    thickness = max(2, int(diameter * _SPINNER_THICKNESS_RATIO))

    label_font = QFont()
    label_font.setPointSize(12)
    metrics = QFontMetrics(label_font)
    label_height = metrics.height()

    block_height = diameter + _SPINNER_GAP_TO_LABEL + label_height
    center_x = rect.center().x()
    block_top = rect.center().y() - block_height / 2.0

    spinner_rect = QRectF(
        center_x - diameter / 2.0,
        block_top,
        float(diameter),
        float(diameter),
    )

    # Soft track ring under the moving arc gives the spinner a familiar
    # "still working" look even when paused on a single frame.
    painter.save()
    track_pen = QPen(QColor(255, 255, 255, 110))
    track_pen.setWidth(thickness)
    track_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(track_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(spinner_rect)

    # Moving arc. Qt's `drawArc` takes 16ths of a degree.
    arc_pen = QPen(QColor("#3a86ff"))
    arc_pen.setWidth(thickness)
    arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(arc_pen)
    start_angle = int((90 - phase_deg) * 16)
    sweep_angle = int(-_SPINNER_ARC_SWEEP * 16)
    painter.drawArc(spinner_rect, start_angle, sweep_angle)
    painter.restore()

    # Drop a faint shadow behind the label so it stays readable on the
    # checkerboard regardless of which cell it lands on.
    painter.save()
    painter.setFont(label_font)
    label_rect = QRectF(
        rect.left(),
        block_top + diameter + _SPINNER_GAP_TO_LABEL,
        rect.width(),
        float(label_height),
    )
    shadow_rect = QRectF(label_rect)
    shadow_rect.translate(0, 1)
    painter.setPen(QColor(0, 0, 0, 90))
    painter.drawText(shadow_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "Loading…")
    painter.setPen(QColor(Theme.color_text_muted))
    painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "Loading…")
    painter.restore()
