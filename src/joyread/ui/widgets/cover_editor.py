"""Book cover editor dialog adapted from Figma node 734:4133."""

from __future__ import annotations

from math import ceil

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.services.thumbnail_service import CoverCropState
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.window_drag import start_window_drag_if_on_drag_handle
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.book_detail import DetailThumbnailGrid
from joyread.ui.widgets.settings_page import SettingsSpinButtonSmall


class CoverEditorOverlay(QWidget):
    closed = QtSignal()
    import_requested = QtSignal()
    thumbnail_interest_changed = QtSignal(tuple, tuple, tuple)
    thumbnail_interest_released = QtSignal()
    picker_visibility_changed = QtSignal(bool)
    thumbnail_selected = QtSignal(int)
    save_requested = QtSignal(object)

    def __init__(
        self,
        resources: ResourceLoader,
        parent: QWidget | None = None,
        *,
        drag_handle: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("CoverEditorOverlay")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._drag_handle = drag_handle

        self._stack = QStackedWidget(self)
        self._stack.setFixedSize(Theme.cover_editor_width, Theme.cover_editor_height)
        self.editor = CoverEditorWidget(resources)
        self.picker = CoverThumbnailPickerWidget(resources)
        self._stack.addWidget(self.editor)
        self._stack.addWidget(self.picker)

        self.editor.import_requested.connect(self.import_requested.emit)
        self.editor.browse_requested.connect(self._show_picker)
        self.editor.cancel_requested.connect(self.hide)
        self.editor.confirm_requested.connect(self._emit_save_requested)
        self.picker.back_requested.connect(self._show_editor)
        self.picker.thumbnail_interest_changed.connect(self.thumbnail_interest_changed.emit)
        self.picker.thumbnail_interest_released.connect(self.thumbnail_interest_released.emit)
        self.picker.thumbnail_selected.connect(self.thumbnail_selected.emit)
        self.hide()

    def refresh_labels(self) -> None:
        self.editor.refresh_labels()
        self.picker.refresh_labels()

    def open_editor(self, frame: QImage, source_id: str) -> bool:
        if not self.editor.set_source(frame, source_id):
            return False
        self._show_editor()
        self._position_panel()
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.PopupFocusReason)
        return True

    def set_source(self, frame: QImage, source_id: str) -> bool:
        if not self.editor.set_source(frame, source_id):
            return False
        self._show_editor()
        return True

    def set_thumbnail_page_count(self, page_count: int) -> None:
        self.picker.set_page_count(page_count)

    def set_thumbnail(self, page_index: int, image_bytes: bytes) -> None:
        self.picker.set_thumbnail(page_index, image_bytes)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        start_window_drag_if_on_drag_handle(event, self._drag_handle)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_panel()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self.picker.release_interest()
        self.picker_visibility_changed.emit(False)
        self.closed.emit()

    def _show_editor(self) -> None:
        was_picker = self._stack.currentWidget() is self.picker
        self.picker.release_interest()
        self._stack.setCurrentWidget(self.editor)
        if was_picker:
            self.picker_visibility_changed.emit(False)
        self._position_panel()
        self.raise_()

    def _show_picker(self) -> None:
        self.picker.reset()
        self._stack.setCurrentWidget(self.picker)
        self._position_panel()
        self.picker_visibility_changed.emit(True)
        self.picker.refresh_interest()

    def _emit_save_requested(self) -> None:
        if not self.editor.has_source:
            return
        self.save_requested.emit(self.editor.crop_state())

    def _position_panel(self) -> None:
        self._stack.move(
            max(0, (self.width() - self._stack.width()) // 2),
            max(0, (self.height() - self._stack.height()) // 2),
        )


class CoverEditorWidget(QFrame):
    import_requested = QtSignal()
    browse_requested = QtSignal()
    cancel_requested = QtSignal()
    confirm_requested = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CoverEditorPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.cover_editor_width, Theme.cover_editor_height)

        root_layout = QVBoxLayout(self)
        # The Figma cover-adjust frame is edge-to-edge in the 360px panel.
        # Qt's 1px stylesheet border consumes content space, so the widget
        # itself spans the full inner rect while preserving the outer stroke.
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(Theme.cover_editor_gap)

        self.canvas = CoverAdjustCanvas()
        self.canvas.zoom_changed.connect(self._sync_zoom_control)
        root_layout.addWidget(self.canvas)

        edit_area = QWidget()
        edit_area.setObjectName("CoverEditorEditArea")
        edit_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        edit_layout = QVBoxLayout(edit_area)
        edit_layout.setContentsMargins(
            Theme.cover_editor_section_padding,
            Theme.cover_editor_section_padding,
            Theme.cover_editor_section_padding,
            Theme.cover_editor_section_padding,
        )
        edit_layout.setSpacing(Theme.cover_editor_controls_gap)
        edit_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        import_row = QWidget()
        import_row.setObjectName("CoverEditorImportRow")
        import_layout = QHBoxLayout(import_row)
        import_layout.setContentsMargins(0, 0, 0, 0)
        import_layout.setSpacing(Theme.cover_editor_import_row_gap)
        import_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._import_button = _CoverTextButton(t("cover_editor.import_image"))
        self._import_button.clicked.connect(self.import_requested.emit)
        import_layout.addWidget(self._import_button)
        self._browse_button = _icon_button("CoverEditorBrowseButton", resources.icon_path("icon_list_cardMode.svg"))
        self._browse_button.clicked.connect(self.browse_requested.emit)
        import_layout.addWidget(self._browse_button)
        edit_layout.addWidget(import_row, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.zoom_spin = CoverZoomSpinButton(
            100,
            1,
            Theme.cover_editor_max_zoom_percent,
            "%",
            resources,
        )
        self.zoom_spin.value_changed.connect(self.canvas.set_zoom_percent)
        edit_layout.addWidget(self.zoom_spin, alignment=Qt.AlignmentFlag.AlignHCenter)

        action_row = QWidget()
        action_row.setObjectName("CoverEditorActionRow")
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(Theme.cover_editor_icon_button_gap)
        action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cancel_button = _icon_button("CoverEditorCancelButton", resources.icon_path("icon_close.svg"))
        self._cancel_button.clicked.connect(self.cancel_requested.emit)
        action_layout.addWidget(self._cancel_button)
        self._confirm_button = _icon_button("CoverEditorConfirmButton", resources.icon_path("icon_confirm.svg"))
        self._confirm_button.clicked.connect(self.confirm_requested.emit)
        action_layout.addWidget(self._confirm_button)
        edit_layout.addWidget(action_row, alignment=Qt.AlignmentFlag.AlignHCenter)

        root_layout.addWidget(edit_area, stretch=1)
        self.refresh_labels()

    @property
    def has_source(self) -> bool:
        return self.canvas.has_source

    def set_source(self, frame: QImage, source_id: str) -> bool:
        if not self.canvas.set_source(frame, source_id):
            return False
        self._sync_zoom_range()
        self._sync_zoom_control(self.canvas.zoom_percent)
        return True

    def crop_state(self) -> CoverCropState:
        return self.canvas.crop_state()

    def refresh_labels(self) -> None:
        self._import_button.set_text(t("cover_editor.import_image"))
        self._import_button.setToolTip(t("cover_editor.import_image"))
        self._browse_button.setToolTip(t("cover_editor.browse_pages"))
        self._cancel_button.setToolTip(t("cover_editor.cancel"))
        self._confirm_button.setToolTip(t("cover_editor.confirm"))

    def _sync_zoom_range(self) -> None:
        self.zoom_spin.set_range(
            ceil(self.canvas.minimum_zoom_percent),
            Theme.cover_editor_max_zoom_percent,
        )

    def _sync_zoom_control(self, value: int) -> None:
        self.zoom_spin.set_value(value, emit=False)


class CoverThumbnailPickerWidget(QFrame):
    thumbnail_selected = QtSignal(int)
    thumbnail_interest_changed = QtSignal(tuple, tuple, tuple)
    thumbnail_interest_released = QtSignal()
    back_requested = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CoverEditorPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.cover_editor_width, Theme.cover_editor_height)
        self._page_count = 0
        self._last_interest: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
        self._interest_timer = QTimer(self)
        self._interest_timer.setSingleShot(True)
        self._interest_timer.timeout.connect(self._emit_interest)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.cover_editor_layout_margin,
            Theme.cover_editor_layout_margin,
            Theme.cover_editor_layout_margin,
            Theme.cover_editor_layout_margin,
        )
        layout.setSpacing(Theme.cover_editor_gap)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("CoverThumbnailScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.viewport().setObjectName("CoverThumbnailViewport")
        self._scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._scroll.verticalScrollBar().valueChanged.connect(self._defer_interest)
        self._scroll.verticalScrollBar().rangeChanged.connect(lambda _minimum, _maximum: self._defer_interest())

        page = QWidget()
        page.setObjectName("CoverThumbnailPickerPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        self._grid = DetailThumbnailGrid(minimum_width=Theme.cover_editor_thumbnail_min_width)
        self._grid.thumbnail_clicked.connect(self.thumbnail_selected.emit)
        page_layout.addWidget(self._grid, alignment=Qt.AlignmentFlag.AlignHCenter)
        page_layout.addStretch(1)
        self._scroll.setWidget(page)
        layout.addWidget(self._scroll, stretch=1)
        self._scroll_handle = AutoHideScrollHandle(self._scroll, parent=self)

        action_row = QWidget()
        action_row.setObjectName("CoverEditorActionRow")
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(0)
        action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._back_button = _icon_button("CoverEditorCancelButton", resources.icon_path("icon_close.svg"))
        self._back_button.clicked.connect(self.back_requested.emit)
        action_layout.addWidget(self._back_button)
        layout.addWidget(action_row)
        self.refresh_labels()

    def refresh_labels(self) -> None:
        self._back_button.setToolTip(t("cover_editor.back"))

    def reset(self) -> None:
        self._grid.set_thumbnail_count(self._page_count, reset=True)
        self._last_interest = ((), ())
        self._scroll.verticalScrollBar().setValue(0)

    def refresh_interest(self) -> None:
        self._last_interest = ((), ())
        self._defer_interest()

    def set_page_count(self, page_count: int) -> None:
        self._page_count = max(0, int(page_count))
        self._grid.set_thumbnail_count(self._page_count, reset=True)
        self._last_interest = ((), ())
        self._defer_interest()

    def set_thumbnail(self, page_index: int, image_bytes: bytes) -> None:
        self._grid.set_thumbnail(page_index, image_bytes)

    def release_interest(self) -> None:
        self._interest_timer.stop()
        self._grid.set_interest(())
        if self._last_interest != ((), ()):
            self.thumbnail_interest_released.emit()
        self._last_interest = ((), ())

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._defer_interest()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self.release_interest()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._defer_interest()

    def _defer_interest(self) -> None:
        if not self._interest_timer.isActive():
            self._interest_timer.start(0)

    def _emit_interest(self) -> None:
        if not self.isVisible() or self._page_count <= 0:
            return
        origin = self._grid.mapFrom(self._scroll.viewport(), QPoint(0, 0))
        viewport_rect = QRect(origin, self._scroll.viewport().size())
        visible, prefetch = self._grid.visible_and_prefetch_indices(viewport_rect, prefetch_rows=1)
        interest = (visible, prefetch)
        if interest == self._last_interest:
            return
        self._last_interest = interest
        self._grid.set_interest((*visible, *prefetch))
        self.thumbnail_interest_changed.emit(
            visible,
            prefetch,
            (Theme.detail_thumbnail_width, Theme.detail_thumbnail_height),
        )


class CoverAdjustCanvas(QFrame):
    zoom_changed = QtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CoverEditorAdjustArea")
        self.setFixedHeight(Theme.cover_editor_adjust_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._pixmap = QPixmap()
        self._source_id = ""
        self._zoom_percent = 100.0
        self._minimum_zoom_percent = 100.0
        self._pan = QPointF(0, 0)
        self._dragging = False
        self._drag_start = QPointF(0, 0)
        self._drag_start_pan = QPointF(0, 0)

    @property
    def zoom_percent(self) -> int:
        return round(self._zoom_percent)

    @property
    def minimum_zoom_percent(self) -> float:
        return self._minimum_zoom_percent

    @property
    def has_source(self) -> bool:
        return not self._pixmap.isNull()

    def set_source(self, frame: QImage, source_id: str) -> bool:
        if not isinstance(frame, QImage) or frame.isNull():
            return False
        self._pixmap = QPixmap.fromImage(frame)
        self._source_id = source_id
        self._pan = QPointF(0, 0)
        self._zoom_percent = 100.0
        self._minimum_zoom_percent = self._calculate_minimum_zoom_percent()
        self._clamp_state()
        self.zoom_changed.emit(self.zoom_percent)
        self.update()
        return True

    def set_zoom_percent(self, value: int) -> None:
        previous = self.zoom_percent
        target = float(value)
        if target <= ceil(self._minimum_zoom_percent):
            target = self._minimum_zoom_percent
        self._zoom_percent = max(
            self._minimum_zoom_percent,
            min(float(Theme.cover_editor_max_zoom_percent), target),
        )
        self._clamp_state()
        current = self.zoom_percent
        if current != previous:
            self.zoom_changed.emit(current)
        self.update()

    def crop_state(self) -> CoverCropState:
        max_x, max_y = self._maximum_pan()
        return CoverCropState(
            source_id=self._source_id,
            zoom_percent=self._zoom_percent,
            pan_x=(self._pan.x() / max_x) if max_x > 0 else 0.0,
            pan_y=(self._pan.y() / max_y) if max_y > 0 else 0.0,
            crop_size=(Theme.cover_width, Theme.cover_height),
        )

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        direction = 1 if delta > 0 else -1
        self.set_zoom_percent(self.zoom_percent + (direction * Theme.cover_editor_zoom_step))
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._pixmap.isNull():
            self._dragging = True
            self._drag_start = event.position()
            self._drag_start_pan = QPointF(self._pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            delta = event.position() - self._drag_start
            self._pan = self._drag_start_pan + delta
            self._clamp_state()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        crop_rect = self._crop_rect()

        if self._pixmap.isNull():
            _draw_checkerboard(painter, crop_rect)
        else:
            target = self._image_target_rect(crop_rect)
            if self._dragging:
                painter.save()
                painter.setOpacity(Theme.cover_editor_outside_opacity)
                painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
                painter.restore()

            painter.save()
            painter.setClipRect(crop_rect)
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            painter.restore()

        pen = QPen(QColor(0, 0, 0), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(crop_rect.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()

    def _crop_rect(self) -> QRectF:
        return QRectF(
            (self.width() - Theme.cover_width) / 2,
            (self.height() - Theme.cover_height) / 2,
            Theme.cover_width,
            Theme.cover_height,
        )

    def _image_target_rect(self, crop_rect: QRectF) -> QRectF:
        scale = self._fill_scale() * self._zoom_percent / 100.0
        width = self._pixmap.width() * scale
        height = self._pixmap.height() * scale
        return QRectF(
            crop_rect.center().x() - (width / 2) + self._pan.x(),
            crop_rect.center().y() - (height / 2) + self._pan.y(),
            width,
            height,
        )

    def _calculate_minimum_zoom_percent(self) -> float:
        if self._pixmap.isNull():
            return 100.0
        fill = self._fill_scale()
        contain = min(Theme.cover_width / self._pixmap.width(), Theme.cover_height / self._pixmap.height())
        return max(1.0, (contain / fill) * 100.0)

    def _fill_scale(self) -> float:
        if self._pixmap.isNull():
            return 1.0
        return max(Theme.cover_width / self._pixmap.width(), Theme.cover_height / self._pixmap.height())

    def _clamp_state(self) -> None:
        if self._pixmap.isNull():
            self._pan = QPointF(0, 0)
            return
        max_x, max_y = self._maximum_pan()
        self._pan = QPointF(
            max(-max_x, min(max_x, self._pan.x())),
            max(-max_y, min(max_y, self._pan.y())),
        )

    def _maximum_pan(self) -> tuple[float, float]:
        if self._pixmap.isNull():
            return 0.0, 0.0
        scale = self._fill_scale() * self._zoom_percent / 100.0
        display_width = self._pixmap.width() * scale
        display_height = self._pixmap.height() * scale
        return (
            max(0.0, (display_width - Theme.cover_width) / 2),
            max(0.0, (display_height - Theme.cover_height) / 2),
        )


class CoverZoomSpinButton(SettingsSpinButtonSmall):
    def set_range(self, minimum: int, maximum: int) -> None:
        self._minimum = max(1, int(minimum))
        self._maximum = max(self._minimum, int(maximum))
        self._value_editor.setMaxLength(max(len(str(self._minimum)), len(str(self._maximum))))
        self.set_value(self.value)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        direction = 1 if delta > 0 else -1
        self.step_by(direction * Theme.cover_editor_zoom_step)
        event.accept()


class _CoverTextButton(QFrame):
    clicked = QtSignal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pressed_inside = False
        self.setObjectName("CoverEditorImportButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.cover_editor_import_button_width, Theme.cover_editor_import_button_height)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(Theme.spacing_xs, Theme.spacing_xs, Theme.spacing_xs, Theme.spacing_xs)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel(text)
        self._label.setObjectName("CoverEditorImportText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)


def _icon_button(object_name: str, icon_path) -> QToolButton:  # noqa: ANN001 - pathlib path from ResourceLoader.
    button = QToolButton()
    button.setObjectName(object_name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setIcon(QIcon(str(icon_path)))
    button.setIconSize(QSize(Theme.cover_editor_icon_size, Theme.cover_editor_icon_size))
    button.setFixedSize(Theme.cover_editor_icon_button_size, Theme.cover_editor_icon_button_size)
    return button


def _draw_checkerboard(painter: QPainter, rect: QRectF) -> None:
    colors = (QColor("#fafafa"), QColor("#efefef"))
    square = 12
    left = int(rect.left())
    top = int(rect.top())
    painter.save()
    painter.setClipRect(rect)
    for y in range(top, int(rect.bottom()) + 1, square):
        for x in range(left, int(rect.right()) + 1, square):
            painter.fillRect(x, y, square, square, colors[((x // square) + (y // square)) % 2])
    painter.restore()
