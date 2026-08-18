"""Reader page canvas that paints loaded archive pages."""

from __future__ import annotations

import logging
from time import perf_counter

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from joyread.app.reader_page_pipeline import PreparedReaderPage
from joyread.core.diagnostics import reader_perf_enabled, reader_perf_event
from joyread.core.reader import ReaderLayoutResult
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)


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
_STATUS_TEXT_MAX_WIDTH = 680.0
_STATUS_TEXT_WIDTH_RATIO = 0.72
_STATUS_TEXT_HEIGHT_RATIO = 0.70
_STATUS_TEXT_PADDING = 16.0
_PERF_HEARTBEAT_INTERVAL_MS = 16
_PERF_STALL_THRESHOLD_MS = 100.0
_PERF_SAMPLE_LIMIT = 4096


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
        self._frame_signatures: dict[int, tuple[int, int, tuple[int, int], float]] = {}
        self._failed_pages: set[int] = set()
        self._pan_x = 0.0
        self._status_text = t("reader.loading")
        # Spinner phase advances while any visible page is still loading. The
        # timer is created lazily and stopped as soon as every draw has its
        # pixmap, so a fully-loaded spread costs nothing.
        self._spinner_phase = 0.0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(_SPINNER_TICK_INTERVAL_MS)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        self._perf_enabled = reader_perf_enabled()
        self._perf_conversion_ms: list[float] = []
        self._perf_paint_ms: list[float] = []
        self._perf_heartbeat_ms: list[float] = []
        self._perf_conversions = 0
        self._perf_duplicate_skips = 0
        self._perf_last_heartbeat = perf_counter()
        self._perf_heartbeat_timer: QTimer | None = None
        if self._perf_enabled:
            self._perf_heartbeat_timer = QTimer(self)
            self._perf_heartbeat_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self._perf_heartbeat_timer.setInterval(_PERF_HEARTBEAT_INTERVAL_MS)
            self._perf_heartbeat_timer.timeout.connect(self._record_heartbeat)
            self._perf_heartbeat_timer.start()

    def set_layout_result(self, result: ReaderLayoutResult | None, pan_x: float = 0.0) -> None:
        self._layout_result = result
        self._pan_x = pan_x
        self._prune_pixmaps_to_layout()
        self._refresh_spinner_state()
        self.update()

    def set_page_frame(self, image: PreparedReaderPage) -> None:
        # Drop pages that are no longer in the current layout. The reader VM
        # may emit ``page_ready`` after the user has already navigated away,
        # and rendering those stale pages would paint last spread's content
        # on top of the new one for a frame. The same defence catches
        # out-of-order arrivals from the worker pool.
        if self._layout_result is not None and not self._layout_draws_page(image.page_index):
            logger.debug("ReaderCanvas drop stale page=%d", image.page_index)
            return
        frame = image.frame
        if isinstance(frame, QImage):
            signature = (
                int(image.generation),
                int(frame.cacheKey()),
                (int(image.rendered_dimensions[0]), int(image.rendered_dimensions[1])),
                round(float(frame.devicePixelRatio()), 4),
            )
            if (
                self._frame_signatures.get(image.page_index) == signature
                and image.page_index in self._pixmaps
            ):
                self._perf_duplicate_skips += 1
                reader_perf_event(
                    "gui.pixmap",
                    page=image.page_index,
                    generation=image.generation,
                    action="duplicate_skipped",
                    rendered=image.rendered_dimensions,
                )
                return
        else:
            signature = None
        started = perf_counter() if self._perf_enabled else 0.0
        pixmap = QPixmap.fromImage(frame) if isinstance(frame, QImage) else QPixmap()
        if self._perf_enabled:
            conversion_ms = (perf_counter() - started) * 1000.0
            self._append_perf_sample(self._perf_conversion_ms, conversion_ms)
            self._perf_conversions += 1
            reader_perf_event(
                "gui.pixmap",
                page=image.page_index,
                generation=image.generation,
                action="converted",
                elapsed_ms=round(conversion_ms, 3),
                rendered=image.rendered_dimensions,
            )
        if not pixmap.isNull():
            self._pixmaps[image.page_index] = pixmap
            if signature is not None:
                self._frame_signatures[image.page_index] = signature
            self._failed_pages.discard(image.page_index)
            self._refresh_spinner_state()
            self.update()
            logger.debug("ReaderCanvas accept page=%d", image.page_index)
        else:
            self.set_page_failed(image.page_index)
            logger.warning("ReaderCanvas received invalid frame page=%d", image.page_index)

    def set_page_failed(self, page_index: int) -> None:
        self._pixmaps.pop(page_index, None)
        self._frame_signatures.pop(page_index, None)
        self._failed_pages.add(page_index)
        self._refresh_spinner_state()
        self.update()

    def clear_pages(self) -> None:
        self._layout_result = None
        self._pixmaps.clear()
        self._frame_signatures.clear()
        self._failed_pages.clear()
        self._pan_x = 0.0
        if self._spinner_timer.isActive():
            self._spinner_timer.stop()
        self.update()

    def set_status_text(self, text: str) -> None:
        self._status_text = text
        self._refresh_status_tooltip()
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
        perf_started = perf_counter() if self._perf_enabled else 0.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        shell_clip = QPainterPath()
        shell_clip.addRoundedRect(QRectF(self.rect()), Theme.reader_radius, Theme.reader_radius)
        painter.setClipPath(shell_clip)
        painter.fillRect(self.rect(), QColor(Theme.color_reader_background))

        if self._layout_result is None or not self._layout_result.page_draws:
            _draw_wrapped_status_text(painter, QRectF(self.rect()), self._status_text)
            painter.end()
            self._record_paint(perf_started)
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
                if draw.page_index in self._failed_pages:
                    _draw_error_page(painter, rect)
                else:
                    _draw_placeholder_page(painter, rect, self._spinner_phase)
            else:
                painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
        painter.end()
        self._record_paint(perf_started)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_status_tooltip()

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
            if (pixmap is None or pixmap.isNull()) and draw.page_index not in self._failed_pages:
                return True
        return False

    def _layout_draws_page(self, page_index: int) -> bool:
        return self._layout_result is not None and any(
            draw.page_index == page_index for draw in self._layout_result.page_draws
        )

    def _prune_pixmaps_to_layout(self) -> None:
        if self._layout_result is None:
            self._pixmaps.clear()
            self._frame_signatures.clear()
            self._failed_pages.clear()
            return
        visible = {draw.page_index for draw in self._layout_result.page_draws}
        for page_index in tuple(self._pixmaps):
            if page_index not in visible:
                self._pixmaps.pop(page_index, None)
                self._frame_signatures.pop(page_index, None)
        self._failed_pages.intersection_update(visible)

    def performance_snapshot(self) -> dict[str, object]:
        """Return debug-only counters used by the performance playground."""

        return {
            "enabled": self._perf_enabled,
            "pixmap_conversions": self._perf_conversions,
            "pixmap_duplicate_skips": self._perf_duplicate_skips,
            "pixmap_conversion_p95_ms": _percentile(self._perf_conversion_ms, 0.95),
            "pixmap_conversion_max_ms": max(self._perf_conversion_ms, default=0.0),
            "paint_p95_ms": _percentile(self._perf_paint_ms, 0.95),
            "paint_max_ms": max(self._perf_paint_ms, default=0.0),
            "heartbeat_p95_ms": _percentile(self._perf_heartbeat_ms, 0.95),
            "heartbeat_max_ms": max(self._perf_heartbeat_ms, default=0.0),
            "resident_pixmaps": len(self._pixmaps),
        }

    def reset_performance_measurements(self) -> None:
        """Start a fresh debug sampling window after cold Reader startup."""

        if not self._perf_enabled:
            return
        self._perf_conversion_ms.clear()
        self._perf_paint_ms.clear()
        self._perf_heartbeat_ms.clear()
        self._perf_conversions = 0
        self._perf_duplicate_skips = 0
        self._perf_last_heartbeat = perf_counter()

    def _record_paint(self, started: float) -> None:
        if not self._perf_enabled:
            return
        elapsed = (perf_counter() - started) * 1000.0
        self._append_perf_sample(self._perf_paint_ms, elapsed)
        reader_perf_event(
            "gui.paint",
            elapsed_ms=round(elapsed, 3),
            pages=len(self._layout_result.page_draws) if self._layout_result is not None else 0,
            canvas=(self.width(), self.height()),
        )

    def _record_heartbeat(self) -> None:
        now = perf_counter()
        gap_ms = (now - self._perf_last_heartbeat) * 1000.0
        self._perf_last_heartbeat = now
        self._append_perf_sample(self._perf_heartbeat_ms, gap_ms)
        if gap_ms >= _PERF_STALL_THRESHOLD_MS:
            reader_perf_event("gui.heartbeat_stall", gap_ms=round(gap_ms, 3))

    @staticmethod
    def _append_perf_sample(samples: list[float], value: float) -> None:
        samples.append(float(value))
        if len(samples) > _PERF_SAMPLE_LIMIT:
            del samples[: len(samples) - _PERF_SAMPLE_LIMIT]

    def _tick_spinner(self) -> None:
        # Phase advances monotonically; ``%`` keeps the value small so the
        # float doesn't lose precision over very long sessions.
        self._spinner_phase = (self._spinner_phase + _SPINNER_PHASE_STEP_DEG) % 360.0
        self.update()

    def _refresh_status_tooltip(self) -> None:
        if self._layout_result is not None:
            self.setToolTip("")
            return
        self.setToolTip(
            self._status_text
            if _status_text_is_clipped(self.fontMetrics(), QRectF(self.rect()), self._status_text)
            else ""
        )


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


def _draw_error_page(painter: QPainter, rect: QRectF) -> None:
    """Static failed state; unlike pending pages it never runs the spinner."""

    painter.save()
    painter.fillRect(rect, QColor("#d8d8d8"))
    font = QFont(painter.font())
    font.setPixelSize(max(18, min(48, round(min(rect.width(), rect.height()) * 0.12))))
    painter.setFont(font)
    painter.setPen(QColor(Theme.color_text_muted))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "!")
    painter.restore()


def _draw_wrapped_status_text(painter: QPainter, rect: QRectF, text: str) -> None:
    painter.save()
    painter.setPen(QColor(Theme.color_text_muted))
    text_rect = _wrapped_status_text_rect(painter.fontMetrics(), rect, text)
    flags = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
    shadow_rect = QRectF(text_rect)
    shadow_rect.translate(0, 1)
    painter.setPen(QColor(0, 0, 0, 80))
    painter.drawText(shadow_rect, flags, text)
    painter.setPen(QColor(Theme.color_text_muted))
    painter.drawText(text_rect, flags, text)
    painter.restore()


def _wrapped_status_text_rect(metrics: QFontMetrics, rect: QRectF, text: str) -> QRectF:
    max_width = max(1.0, min(_STATUS_TEXT_MAX_WIDTH, rect.width() * _STATUS_TEXT_WIDTH_RATIO))
    max_height = max(1.0, rect.height() * _STATUS_TEXT_HEIGHT_RATIO)
    bounds = metrics.boundingRect(
        QRect(0, 0, int(max_width), int(max_height)),
        int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
        text,
    )
    width = min(max_width, max(float(bounds.width()) + _STATUS_TEXT_PADDING, 1.0))
    height = min(max_height, max(float(bounds.height()) + _STATUS_TEXT_PADDING, float(metrics.height())))
    return QRectF(
        rect.center().x() - (width / 2.0),
        rect.center().y() - (height / 2.0),
        width,
        height,
    )


def _status_text_is_clipped(metrics: QFontMetrics, rect: QRectF, text: str) -> bool:
    max_width = max(1.0, min(_STATUS_TEXT_MAX_WIDTH, rect.width() * _STATUS_TEXT_WIDTH_RATIO))
    max_height = max(1.0, rect.height() * _STATUS_TEXT_HEIGHT_RATIO)
    bounds = metrics.boundingRect(
        QRect(0, 0, int(max_width), 1000000),
        int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
        text,
    )
    return (float(bounds.width()) + _STATUS_TEXT_PADDING) > max_width or (
        float(bounds.height()) + _STATUS_TEXT_PADDING
    ) > max_height


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return float(ordered[index])


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
    painter.drawText(shadow_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, t("reader.loading"))
    painter.setPen(QColor(Theme.color_text_muted))
    painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, t("reader.loading"))
    painter.restore()
