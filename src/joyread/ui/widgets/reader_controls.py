"""Figma-aligned controls used by the reader window."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.reader import ReaderDirection, ReaderTransitionMode
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.window_chrome import WindowControlsWidget


class ReaderHeader(QWidget):
    back_requested = QtSignal()
    mouse_activity = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
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

        back = reader_button(resources, "icon_left.svg", "Back")
        back.setProperty("class", "ChromeButton")
        back.clicked.connect(self.back_requested.emit)
        layout.addWidget(back)
        layout.addWidget(_spacer(height=Theme.reader_control_size))

        self.mode_group = QFrame()
        self.mode_group.setProperty("class", "ReaderTopGroup")
        shadow = QGraphicsDropShadowEffect(self.mode_group)
        shadow.setBlurRadius(4)
        shadow.setOffset(0, 0)
        shadow.setColor(QColor(*Theme.color_shadow_rgba))
        self.mode_group.setGraphicsEffect(shadow)
        mode_layout = QHBoxLayout(self.mode_group)
        # Figma uses 4px visual padding and a 2px stroke here. Qt borders
        # consume layout space, so 2px margins preserve the visual inset.
        mode_layout.setContentsMargins(2, 2, 2, 2)
        mode_layout.setSpacing(Theme.reader_switch_gap)
        self.detail_button = switch_option(resources, "icon_list_detailMode.svg", "Details")
        self.detail_button.setEnabled(False)
        self.bookmark_button = switch_option(resources, "icon_bookmark.svg", "Bookmarks")
        self.thumbnail_button = switch_option(resources, "icon_list_cardMode.svg", "Thumbnails")
        self.thumbnail_button.setChecked(True)
        mode_layout.addWidget(self.detail_button)
        mode_layout.addWidget(self.bookmark_button)
        mode_layout.addWidget(_spacer(height=20))
        mode_layout.addWidget(self.thumbnail_button)
        layout.addWidget(self.mode_group)

        layout.addStretch(1)
        self.title = QLabel("Place Holder - Book Name", self)
        self.title.setObjectName("ReaderTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title.setFixedHeight(Theme.reader_control_size)
        layout.addStretch(1)
        layout.addWidget(QWidget(), stretch=0)

    def set_title(self, title: str) -> None:
        self._full_title = title
        self._position_title()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_title()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.mouse_activity.emit()
        super().enterEvent(event)

    def _position_title(self) -> None:
        max_width = max(80, self.width() - 360)
        metrics = QFontMetrics(self.title.font())
        self.title.setFixedWidth(max_width)
        self.title.setText(metrics.elidedText(self._full_title, Qt.TextElideMode.ElideRight, max_width))
        self.title.setToolTip(self._full_title if self.title.text() != self._full_title else "")
        self.title.move((self.width() - self.title.width()) // 2, (self.height() - self.title.height()) // 2)
        self.title.raise_()

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
        layout.setSpacing(0)

        upper = QWidget()
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
        left_layout.addWidget(reader_button(resources, "icon_go-left-end.svg", "Jump to start", self.start_requested.emit))
        left_layout.addWidget(reader_button(resources, "icon_left.svg", "Previous page", self.previous_requested.emit))
        upper_layout.addWidget(left)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("ReaderProgressSlider")
        self.slider.setFixedHeight(Theme.reader_slider_height)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.sliderReleased.connect(lambda: self.seek_requested.emit(self.slider.value()))
        upper_layout.addWidget(self.slider, stretch=1)

        right = QWidget()
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(reader_button(resources, "icon_right.svg", "Next page", self.next_requested.emit))
        right_layout.addWidget(reader_button(resources, "icon_go-right-end.svg", "Jump to end", self.end_requested.emit))
        upper_layout.addWidget(right)
        layout.addWidget(upper)

        lower = QWidget()
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
                ("right", "icon_read-from-right.svg", ReaderDirection.RIGHT_TO_LEFT),
                ("left", "icon_read-from-left.svg", ReaderDirection.LEFT_TO_RIGHT),
                ("top", "icon_read-from-top.svg", ReaderDirection.TOP_TO_BOTTOM),
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
        self.shift_button.setCheckable(True)
        lower_layout.addWidget(self.shift_button)
        lower_layout.addWidget(reader_button(resources, "icon_setting.svg", "Reader settings", self.settings_requested.emit))
        layout.addWidget(lower)

    def set_page_state(self, current_index: int, page_count: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(0, page_count - 1))
        self.slider.setValue(max(0, min(current_index, max(0, page_count - 1))))
        self.slider.blockSignals(False)

    def set_direction(self, direction: ReaderDirection) -> None:
        self.direction_switch.set_value(direction)

    def set_transition_mode(self, mode: ReaderTransitionMode) -> None:
        self.effect_switch.set_value(mode)

    def set_spread_shifted(self, shifted: bool) -> None:
        self.shift_button.setChecked(shifted)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.mouse_activity.emit()
        super().enterEvent(event)


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
    button.setIcon(QIcon(str(resources.icon_path(icon_name))))
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
    button.setCheckable(True)
    button.setIcon(QIcon(str(resources.icon_path(icon_name))))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_switch_option_size, Theme.reader_switch_option_size)
    return button


def _spacer(height: int) -> QFrame:
    frame = QFrame()
    frame.setObjectName("ToolbarSpacer")
    frame.setFixedSize(Theme.toolbar_spacer_width, height)
    frame.setFrameShape(QFrame.Shape.NoFrame)
    return frame
