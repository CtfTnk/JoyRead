"""Figma-aligned controls used by the reader window."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QRectF, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QLinearGradient, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.reader import ReaderDirection, ReaderTransitionMode
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode
from joyread.ui.widgets.window_chrome import WindowControlsWidget


logger = logging.getLogger(__name__)


class ReaderTopicButtonGroup(QFrame):
    """Figma topic-button-group: action buttons with one active panel topic."""

    topic_requested = QtSignal(object)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "ReaderTopicButtonGroup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.reader_topic_button_group_width, Theme.reader_topic_button_group_height)
        self._active_mode: ReaderTopicMode | None = None
        self._buttons: dict[ReaderTopicMode, QToolButton] = {}
        self._effects: dict[ReaderTopicMode, QGraphicsOpacityEffect] = {}
        self._enabled: dict[ReaderTopicMode, bool] = {
            ReaderTopicMode.CONTENTS: False,
            ReaderTopicMode.BOOKMARKS: True,
            ReaderTopicMode.THUMBNAILS: True,
        }

        layout = QHBoxLayout(self)
        # Figma uses 4px visual padding and a 2px stroke. Qt borders consume
        # layout space, so 2px margins preserve the outside-to-icon inset.
        layout.setContentsMargins(
            Theme.reader_topic_button_group_layout_margin,
            Theme.reader_topic_button_group_layout_margin,
            Theme.reader_topic_button_group_layout_margin,
            Theme.reader_topic_button_group_layout_margin,
        )
        layout.setSpacing(Theme.reader_topic_button_group_gap)

        self.contents_button = self._make_button(
            resources,
            ReaderTopicMode.CONTENTS,
            "icon_list_detailMode.svg",
            "Contents",
        )
        self.bookmark_button = self._make_button(
            resources,
            ReaderTopicMode.BOOKMARKS,
            "icon_bookmark.svg",
            "Bookmarks",
        )
        self.thumbnail_button = self._make_button(
            resources,
            ReaderTopicMode.THUMBNAILS,
            "icon_list_cardMode.svg",
            "Thumbnails",
        )
        layout.addWidget(self.contents_button)
        layout.addWidget(self.bookmark_button)
        layout.addWidget(_spacer(height=Theme.reader_topic_button_separator_height))
        layout.addWidget(self.thumbnail_button)
        self.set_contents_enabled(False)

    @property
    def active_mode(self) -> ReaderTopicMode | None:
        return self._active_mode

    def set_contents_enabled(self, enabled: bool) -> None:
        self._set_enabled(ReaderTopicMode.CONTENTS, enabled)

    def set_bookmarks_enabled(self, enabled: bool) -> None:
        self._set_enabled(ReaderTopicMode.BOOKMARKS, enabled)

    def set_active_mode(self, mode: ReaderTopicMode | None) -> None:
        if mode is not None and not self._enabled.get(mode, False):
            mode = None
        if mode == self._active_mode:
            return
        self._active_mode = mode
        for option_mode, button in self._buttons.items():
            button.setProperty("topicActive", option_mode == mode)
            _refresh_style(button)

    def clear_active_mode(self) -> None:
        self.set_active_mode(None)

    def _make_button(
        self,
        resources: ResourceLoader,
        mode: ReaderTopicMode,
        icon_name: str,
        tooltip: str,
    ) -> QToolButton:
        button = topic_button(resources, icon_name, tooltip)
        button.clicked.connect(lambda _checked=False, mode=mode: self._request_mode(mode))
        effect = QGraphicsOpacityEffect(button)
        effect.setOpacity(1.0)
        button.setGraphicsEffect(effect)
        self._buttons[mode] = button
        self._effects[mode] = effect
        return button

    def _request_mode(self, mode: ReaderTopicMode) -> None:
        if not self._enabled.get(mode, False):
            return
        self.set_active_mode(mode)
        self.topic_requested.emit(mode)

    def _set_enabled(self, mode: ReaderTopicMode, enabled: bool) -> None:
        self._enabled[mode] = enabled
        button = self._buttons.get(mode)
        effect = self._effects.get(mode)
        if button is not None:
            button.setEnabled(enabled)
        if effect is not None:
            effect.setOpacity(1.0 if enabled else 0.3)
        if not enabled and self._active_mode == mode:
            self.set_active_mode(None)


class ReaderHeader(QWidget):
    back_requested = QtSignal()
    mouse_activity = QtSignal()
    topic_mode_requested = QtSignal(object)
    custom_requested = QtSignal()

    def __init__(
        self,
        resources: ResourceLoader,
        parent: QWidget | None = None,
        *,
        show_custom_button: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ReaderHeader")
        self.setMouseTracking(True)
        self.setFixedHeight(Theme.reader_banner_height)
        self._full_title = "Place Holder - Book Name"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 8, 8)
        layout.setSpacing(10)

        controls = WindowControlsWidget()
        controls.close_requested.connect(lambda: self.window().close())
        controls.minimize_requested.connect(lambda: self.window().showMinimized())
        controls.zoom_requested.connect(self._toggle_zoom)
        layout.addWidget(controls)

        self.back_button = reader_button(resources, "icon_left.svg", "Back")
        self.back_button.setProperty("class", "ChromeButton")
        self.back_button.clicked.connect(self.back_requested.emit)
        layout.addWidget(self.back_button)
        self.back_spacer = _spacer(height=Theme.reader_control_size)
        layout.addWidget(self.back_spacer)

        self.topic_button_group = ReaderTopicButtonGroup(resources)
        self.topic_button_group.topic_requested.connect(self.topic_mode_requested.emit)
        self.mode_group = self.topic_button_group
        self.detail_button = self.topic_button_group.contents_button
        self.bookmark_button = self.topic_button_group.bookmark_button
        self.thumbnail_button = self.topic_button_group.thumbnail_button
        layout.addWidget(self.topic_button_group)

        layout.addStretch(1)
        self.title = QLabel("Place Holder - Book Name", self)
        self.title.setObjectName("ReaderTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title.setFixedHeight(Theme.reader_control_size)
        layout.addStretch(1)
        # Optional gear at the right edge for the novel reader's Custom
        # panel. Manga passes ``show_custom_button=False`` so its layout
        # is unchanged (a zero-size spacer keeps title-centering symmetry).
        self.custom_button = reader_button(
            resources,
            "icon_setting.svg",
            "Custom",
            self.custom_requested.emit,
        )
        self.custom_button.setVisible(show_custom_button)
        if show_custom_button:
            layout.addWidget(self.custom_button)
        else:
            layout.addWidget(QWidget(), stretch=0)

    def set_back_visible(self, visible: bool) -> None:
        self.back_button.setVisible(visible)
        self.back_spacer.setVisible(visible)
        self._position_title()

    def set_title(self, title: str) -> None:
        self._full_title = title
        self._position_title()

    def set_contents_enabled(self, enabled: bool) -> None:
        self.topic_button_group.set_contents_enabled(enabled)

    def set_bookmarks_enabled(self, enabled: bool) -> None:
        self.topic_button_group.set_bookmarks_enabled(enabled)

    def set_topic_active_mode(self, mode: ReaderTopicMode | None) -> None:
        logger.debug(
            "ReaderHeader topic mode=%s", mode.value if mode is not None else None
        )
        self.topic_button_group.set_active_mode(mode)

    def clear_topic_active_mode(self) -> None:
        self.topic_button_group.clear_active_mode()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_title()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.mouse_activity.emit()
        super().enterEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0, self.height(), 0, 0)
        gradient.setColorAt(0.0, QColor(255, 255, 255, 51))
        gradient.setColorAt(0.5, QColor(255, 255, 255, 204))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 204))
        painter.fillPath(_top_rounded_path(self.rect()), gradient)
        painter.end()

    def _position_title(self) -> None:
        left_edge = self.topic_button_group.geometry().right() + 12 if self.topic_button_group.width() else 160
        right_edge = 52 + 12
        safe_side = max(left_edge, right_edge)
        available = self.width() - (safe_side * 2)
        was_visible = getattr(self, "_title_visible", True)
        if available < 40:
            if was_visible:
                logger.debug("ReaderHeader title hidden (header width=%d)", self.width())
            self._title_visible = False
            self.title.hide()
            return
        metrics = QFontMetrics(self.title.font())
        self.title.setFixedWidth(available)
        self.title.setText(metrics.elidedText(self._full_title, Qt.TextElideMode.ElideRight, available))
        self.title.setToolTip(self._full_title if self.title.text() != self._full_title else "")
        self.title.move((self.width() - self.title.width()) // 2, (self.height() - self.title.height()) // 2)
        self.title.raise_()
        self.title.show()
        if not was_visible:
            logger.debug("ReaderHeader title shown (header width=%d)", self.width())
        self._title_visible = True

    def _toggle_zoom(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()


class ReaderFooter(QWidget):
    start_requested = QtSignal()
    previous_requested = QtSignal()
    next_requested = QtSignal()
    end_requested = QtSignal()
    seek_requested = QtSignal(int)
    direction_changed = QtSignal(object)
    transition_changed = QtSignal(object)
    spread_shift_requested = QtSignal()
    settings_requested = QtSignal()
    mouse_activity = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ReaderFooter")
        self.setMouseTracking(True)
        self.setFixedHeight(Theme.reader_footer_height)
        self._resources = resources

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_footer_padding_horizontal,
            Theme.reader_footer_padding_vertical,
            Theme.reader_footer_padding_horizontal,
            Theme.reader_footer_padding_vertical,
        )
        layout.setSpacing(8)

        upper = QWidget()
        upper.setFixedHeight(Theme.reader_footer_row_height)
        upper_layout = QHBoxLayout(upper)
        upper_layout.setContentsMargins(
            Theme.reader_footer_row_padding_horizontal,
            Theme.reader_footer_row_padding_vertical,
            Theme.reader_footer_row_padding_horizontal,
            Theme.reader_footer_row_padding_vertical,
        )
        upper_layout.setSpacing(10)
        left = QWidget()
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.left_outer_button = reader_button(resources, "icon_go-left-end.svg", "Jump to start", self.start_requested.emit)
        self.left_inner_button = reader_button(resources, "icon_left.svg", "Previous page", self.previous_requested.emit)
        left_layout.addWidget(self.left_outer_button)
        left_layout.addWidget(self.left_inner_button)
        upper_layout.addWidget(left)

        progress_part = QWidget()
        progress_layout = QVBoxLayout(progress_part)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(Theme.reader_progress_indicator_gap)

        self.slider = ReaderProgressSlider()
        self.slider.setFixedHeight(Theme.reader_slider_height)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.sliderReleased.connect(lambda: self.seek_requested.emit(self.slider.value()))
        progress_layout.addWidget(self.slider)

        self.page_indicator = QLabel("0/0")
        self.page_indicator.setObjectName("ReaderProgressIndicator")
        self.page_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_indicator.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        progress_layout.addWidget(self.page_indicator, alignment=Qt.AlignmentFlag.AlignHCenter)
        upper_layout.addWidget(progress_part, stretch=1)

        right = QWidget()
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        self.right_inner_button = reader_button(resources, "icon_right.svg", "Next page", self.next_requested.emit)
        self.right_outer_button = reader_button(resources, "icon_go-right-end.svg", "Jump to end", self.end_requested.emit)
        right_layout.addWidget(self.right_inner_button)
        right_layout.addWidget(self.right_outer_button)
        upper_layout.addWidget(right)
        layout.addWidget(upper)

        lower = QWidget()
        lower.setFixedHeight(Theme.reader_footer_row_height)
        lower_layout = QHBoxLayout(lower)
        lower_layout.setContentsMargins(
            Theme.reader_footer_row_padding_horizontal,
            Theme.reader_footer_row_padding_vertical,
            Theme.reader_footer_row_padding_horizontal,
            Theme.reader_footer_row_padding_vertical,
        )
        lower_layout.setSpacing(6)
        self.direction_switch = ReaderSwitch(
            resources,
            (
                ("Right-to-left", "icon_read-from-left.svg", ReaderDirection.RIGHT_TO_LEFT),
                ("Left-to-right", "icon_read-from-right.svg", ReaderDirection.LEFT_TO_RIGHT),
                ("Top-to-down", "icon_read-from-top.svg", ReaderDirection.TOP_TO_BOTTOM),
            ),
        )
        self.direction_switch.value_changed.connect(self.direction_changed.emit)
        lower_layout.addWidget(self.direction_switch)
        self.effect_switch = ReaderSwitch(
            resources,
            (
                ("none", "icon_change-page_no-effect.svg", ReaderTransitionMode.NONE),
                ("slide", "icon_change-page_slide.svg", ReaderTransitionMode.SLIDE),
            ),
        )
        self.effect_switch.value_changed.connect(self.transition_changed.emit)
        lower_layout.addWidget(self.effect_switch)
        lower_layout.addStretch(1)
        self.shift_button = reader_button(
            resources,
            "icon_shift-by-one.svg",
            "Shift spread pairing",
            self.spread_shift_requested.emit,
        )
        lower_layout.addWidget(self.shift_button)
        self.settings_button = reader_button(resources, "icon_setting.svg", "Reader settings", self.settings_requested.emit)
        lower_layout.addWidget(self.settings_button)
        layout.addWidget(lower)

    def set_page_state(self, current_index: int, page_count: int, direction: ReaderDirection) -> None:
        maximum_index = max(0, page_count - 1)
        safe_index = max(0, min(current_index, maximum_index))
        self.slider.blockSignals(True)
        self.slider.set_reading_direction(direction)
        self.slider.setMaximum(maximum_index)
        self.slider.setValue(safe_index)
        self.slider.blockSignals(False)
        self.page_indicator.setText("0/0" if page_count <= 0 else f"{safe_index + 1}/{page_count}")

    def set_direction(self, direction: ReaderDirection) -> None:
        self.direction_switch.set_value(direction)
        self.slider.set_reading_direction(direction)
        if direction == ReaderDirection.RIGHT_TO_LEFT:
            self.left_outer_button.setToolTip("Jump to end")
            self.left_inner_button.setToolTip("Next page")
            self.right_inner_button.setToolTip("Previous page")
            self.right_outer_button.setToolTip("Jump to start")
        else:
            self.left_outer_button.setToolTip("Jump to start")
            self.left_inner_button.setToolTip("Previous page")
            self.right_inner_button.setToolTip("Next page")
            self.right_outer_button.setToolTip("Jump to end")

    def set_transition_mode(self, mode: ReaderTransitionMode) -> None:
        self.effect_switch.set_value(mode)

    def is_slider_active(self) -> bool:
        return self.slider.isSliderDown()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.mouse_activity.emit()
        super().enterEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(255, 255, 255, 38))
        gradient.setColorAt(0.55, QColor(255, 255, 255, 188))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 210))
        painter.fillPath(_bottom_rounded_path(self.rect()), gradient)
        painter.end()


class ReaderProgressSlider(QSlider):
    """Paint the reader progress track so archive order can mirror by direction."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName("ReaderProgressSlider")
        self._reading_direction = ReaderDirection.RIGHT_TO_LEFT
        self.set_reading_direction(self._reading_direction)

    @property
    def reading_direction(self) -> ReaderDirection:
        return self._reading_direction

    def set_reading_direction(self, direction: ReaderDirection) -> None:
        self._reading_direction = direction
        inverted = direction == ReaderDirection.RIGHT_TO_LEFT
        self.setInvertedAppearance(inverted)
        self.setInvertedControls(inverted)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        track = self._track_rect()
        track_radius = Theme.reader_slider_track_height / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*Theme.color_reader_slider_empty_rgba))
        painter.drawRoundedRect(track, track_radius, track_radius)

        filled = self._filled_track_rect()
        if filled.width() > 0.5:
            painter.setBrush(QColor(*Theme.color_reader_slider_filled_rgba))
            painter.drawRoundedRect(filled, track_radius, track_radius)

        handle = self._handle_rect()
        handle_radius = min(Theme.reader_slider_knob_height, Theme.reader_slider_track_height) / 2
        painter.setBrush(QColor(Theme.color_window))
        painter.setPen(QPen(QColor(Theme.color_button_inner_edge), 1))
        painter.drawRoundedRect(handle, handle_radius, handle_radius)
        painter.end()

    def _track_rect(self) -> QRectF:
        handle_margin = Theme.reader_slider_knob_width / 2
        return QRectF(
            handle_margin,
            (self.height() - Theme.reader_slider_track_height) / 2,
            max(0, self.width() - Theme.reader_slider_knob_width),
            Theme.reader_slider_track_height,
        )

    def _filled_track_rect(self) -> QRectF:
        track = self._track_rect()
        center_x = self._handle_center_x()
        if self._reading_direction == ReaderDirection.RIGHT_TO_LEFT:
            return QRectF(center_x, track.y(), max(0, track.right() - center_x), track.height())
        return QRectF(track.left(), track.y(), max(0, center_x - track.left()), track.height())

    def _handle_rect(self) -> QRectF:
        return QRectF(
            self._handle_center_x() - (Theme.reader_slider_knob_width / 2),
            (self.height() - Theme.reader_slider_knob_height) / 2,
            Theme.reader_slider_knob_width,
            Theme.reader_slider_knob_height,
        )

    def _handle_center_x(self) -> float:
        track = self._track_rect()
        minimum = self.minimum()
        maximum = self.maximum()
        fraction = 0.0 if maximum <= minimum else (self.value() - minimum) / (maximum - minimum)
        if self._reading_direction == ReaderDirection.RIGHT_TO_LEFT:
            fraction = 1.0 - fraction
        return track.left() + (track.width() * max(0.0, min(1.0, fraction)))


class ReaderSwitch(QFrame):
    value_changed = QtSignal(object)

    def __init__(
        self,
        resources: ResourceLoader,
        options: tuple[tuple[str, str, object], ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("class", "ReaderSwitch")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._buttons: dict[object, QToolButton] = {}
        self._value = options[0][2]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_switch_visual_padding,
            Theme.reader_switch_visual_padding,
            Theme.reader_switch_visual_padding,
            Theme.reader_switch_visual_padding,
        )
        layout.setSpacing(Theme.reader_switch_gap)
        for tooltip, icon_name, value in options:
            button = switch_option(resources, icon_name, tooltip)
            button.clicked.connect(lambda _checked=False, value=value: self.set_value(value, emit=True))
            self._buttons[value] = button
            layout.addWidget(button)
        self.set_value(self._value, emit=False)

    @property
    def value(self) -> object:
        return self._value

    def set_value(self, value: object, emit: bool = False) -> None:
        if value not in self._buttons:
            return
        changed = value != self._value
        self._value = value
        for option_value, button in self._buttons.items():
            button.setChecked(option_value == value)
        if changed and emit:
            self.value_changed.emit(value)


def reader_button(
    resources: ResourceLoader,
    icon_name: str,
    tooltip: str,
    callback: Callable[[], None] | None = None,
) -> QToolButton:
    button = QToolButton()
    button.setProperty("class", "ReaderButton")
    button.setProperty("iconName", icon_name)
    button.setIcon(_reader_icon(resources, icon_name))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_control_size, Theme.reader_control_size)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def switch_option(resources: ResourceLoader, icon_name: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setProperty("class", "ReaderSwitchOption")
    button.setProperty("iconName", icon_name)
    button.setCheckable(True)
    button.setIcon(_reader_icon(resources, icon_name))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_switch_option_size, Theme.reader_switch_option_size)
    return button


def topic_button(resources: ResourceLoader, icon_name: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setProperty("class", "ReaderTopicButton")
    button.setProperty("iconName", icon_name)
    button.setProperty("topicActive", False)
    button.setCheckable(False)
    button.setIcon(_reader_icon(resources, icon_name))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_topic_button_size, Theme.reader_topic_button_size)
    return button


def _reader_icon(resources: ResourceLoader, icon_name: str) -> QIcon:
    icon_path = str(resources.icon_path(icon_name))
    icon = QIcon()
    # Qt requests different QIcon modes/states while hovering and checking
    # QToolButtons. Supplying the same SVG for each state avoids transient
    # empty pixmaps when the cursor leaves a reader switch option.
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected, QIcon.Mode.Disabled):
        for state in (QIcon.State.Off, QIcon.State.On):
            icon.addFile(icon_path, QSize(), mode, state)
    return icon


def _refresh_style(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def _spacer(height: int) -> QFrame:
    frame = QFrame()
    frame.setObjectName("ToolbarSpacer")
    frame.setFixedSize(Theme.toolbar_spacer_width, height)
    frame.setFrameShape(QFrame.Shape.NoFrame)
    return frame


def _top_rounded_path(rect) -> QPainterPath:  # noqa: ANN001
    bounds = QRectF(rect)
    radius = _panel_radius(bounds)
    path = QPainterPath()
    # Build one continuous shape. Overlapping addRect/addRoundedRect paths can
    # cancel under Qt's fill rule and leave the glass panel visually missing.
    path.moveTo(bounds.left(), bounds.bottom())
    path.lineTo(bounds.left(), bounds.top() + radius)
    path.quadTo(bounds.left(), bounds.top(), bounds.left() + radius, bounds.top())
    path.lineTo(bounds.right() - radius, bounds.top())
    path.quadTo(bounds.right(), bounds.top(), bounds.right(), bounds.top() + radius)
    path.lineTo(bounds.right(), bounds.bottom())
    path.closeSubpath()
    return path


def _bottom_rounded_path(rect) -> QPainterPath:  # noqa: ANN001
    bounds = QRectF(rect)
    radius = _panel_radius(bounds)
    path = QPainterPath()
    path.moveTo(bounds.left(), bounds.top())
    path.lineTo(bounds.right(), bounds.top())
    path.lineTo(bounds.right(), bounds.bottom() - radius)
    path.quadTo(bounds.right(), bounds.bottom(), bounds.right() - radius, bounds.bottom())
    path.lineTo(bounds.left() + radius, bounds.bottom())
    path.quadTo(bounds.left(), bounds.bottom(), bounds.left(), bounds.bottom() - radius)
    path.lineTo(bounds.left(), bounds.top())
    path.closeSubpath()
    return path


def _panel_radius(bounds: QRectF) -> float:
    return max(0.0, min(float(Theme.reader_radius), bounds.width() / 2, bounds.height()))
