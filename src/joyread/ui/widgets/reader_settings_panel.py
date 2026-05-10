"""Reader-local settings side panel adapted from Figma node 315:5888."""

from __future__ import annotations

import re
from collections.abc import Callable

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.reader import ReaderFitMode, ReaderSettings
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.menus import FigmaMenu


class ReaderSettingsPanel(QFrame):
    custom_enabled_changed = QtSignal(bool)
    always_one_page_changed = QtSignal(bool)
    fit_mode_changed = QtSignal(object)
    vertical_custom_enabled_changed = QtSignal(bool)
    page_spacing_changed = QtSignal(int)
    zoom_percent_changed = QtSignal(int)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ReaderSettingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(Theme.reader_settings_panel_width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
        )
        layout.setSpacing(Theme.reader_settings_gap)

        layout.addWidget(_SectionBanner("Horizontal Mode", resources))

        self.custom_switch = _SmallSwitch()
        self.custom_switch.toggled.connect(self.custom_enabled_changed.emit)
        layout.addWidget(_SettingRow("Enable Custom", self.custom_switch))

        self.one_page_switch = _SmallSwitch()
        self.one_page_switch.toggled.connect(self.always_one_page_changed.emit)
        self.one_page_row = _SettingRow("Single Page", self.one_page_switch)
        layout.addWidget(self.one_page_row)

        self.fit_dropdown = _FitModeButton(resources)
        self.fit_dropdown.value_changed.connect(self.fit_mode_changed.emit)
        self.fit_row = _SettingRow("Fit Mode", self.fit_dropdown, option_margin=0)
        layout.addWidget(self.fit_row)

        layout.addWidget(_SectionBanner("Vertical Mode", resources))

        self.vertical_switch = _SmallSwitch()
        self.vertical_switch.toggled.connect(self.vertical_custom_enabled_changed.emit)
        layout.addWidget(_SettingRow("Enable Custom", self.vertical_switch))

        self.spacing_control = _SpinButton(resources, suffix="px", value=0, minimum=0, maximum=200)
        self.spacing_control.value_changed.connect(self.page_spacing_changed.emit)
        self.spacing_row = _SettingRow("Gap", self.spacing_control, option_margin=0)
        layout.addWidget(self.spacing_row)

        self.zoom_control = _ZoomButton(value=100, minimum=25, maximum=200)
        self.zoom_control.value_changed.connect(self.zoom_percent_changed.emit)
        self.zoom_row = _SettingRow("Zoom", self.zoom_control, option_margin=0)
        layout.addWidget(self.zoom_row)

        layout.addStretch(1)

    def set_settings(self, settings: ReaderSettings) -> None:
        self.custom_switch.set_checked(settings.custom_enabled, emit=False)
        self.one_page_switch.set_checked(settings.always_one_page, emit=False)
        self.fit_dropdown.set_value(settings.fit_mode, emit=False)
        self.vertical_switch.set_checked(settings.vertical_custom_enabled, emit=False)
        self.spacing_control.set_value(settings.page_spacing, emit=False)
        self.zoom_control.set_value(settings.vertical_zoom_percent, emit=False)
        self._sync_child_enabled(settings.custom_enabled)
        self._sync_vertical_child_enabled(settings.vertical_custom_enabled)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*Theme.color_reader_settings_background_rgba))
        painter.drawPath(_right_rounded_path(QRectF(self.rect())))
        painter.end()

    def _sync_child_enabled(self, custom_enabled: bool) -> None:
        self.one_page_row.set_row_enabled(custom_enabled)
        self.fit_row.set_row_enabled(custom_enabled)

    def _sync_vertical_child_enabled(self, custom_enabled: bool) -> None:
        self.spacing_row.set_row_enabled(custom_enabled)
        self.zoom_row.set_row_enabled(custom_enabled)


class _SectionBanner(QFrame):
    def __init__(self, text: str, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "ReaderSettingsSection")
        self.setFixedHeight(Theme.reader_settings_section_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_settings_section_padding_left,
            Theme.reader_settings_section_padding_top,
            Theme.reader_settings_section_padding_right,
            Theme.reader_settings_section_padding_bottom,
        )
        layout.setSpacing(0)

        label = QLabel(text)
        label.setProperty("class", "ReaderSettingsSectionText")
        layout.addWidget(label, stretch=1)

        icon = QLabel()
        icon.setFixedSize(Theme.reader_settings_section_arrow_size, Theme.reader_settings_section_arrow_size)
        icon.setPixmap(
            QIcon(str(resources.icon_path("icon_dropout.svg"))).pixmap(
                QSize(Theme.reader_settings_section_arrow_size, Theme.reader_settings_section_arrow_size)
            )
        )
        layout.addWidget(icon)


class _SettingRow(QFrame):
    def __init__(
        self,
        label: str,
        option: QWidget,
        *,
        option_margin: int = Theme.reader_settings_switch_option_margin,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("class", "ReaderSettingsRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(Theme.reader_settings_row_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_settings_row_padding,
            Theme.reader_settings_row_padding,
            Theme.reader_settings_row_padding,
            Theme.reader_settings_row_padding,
        )
        layout.setSpacing(0)

        name = QWidget()
        name.setFixedHeight(Theme.reader_settings_option_height)
        name_layout = QHBoxLayout(name)
        name_layout.setContentsMargins(
            Theme.reader_settings_name_padding,
            0,
            Theme.reader_settings_name_padding,
            0,
        )
        name_layout.setSpacing(0)
        text = QLabel(label)
        text.setProperty("class", "ReaderSettingsLabel")
        name_layout.addWidget(text)
        layout.addWidget(name, stretch=1)

        option_frame = QWidget()
        option_frame.setFixedHeight(Theme.reader_settings_option_height)
        option_layout = QHBoxLayout(option_frame)
        option_layout.setContentsMargins(option_margin, 0, option_margin, 0)
        option_layout.setSpacing(0)
        option_layout.addWidget(option, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(option_frame, stretch=0)

    def set_row_enabled(self, enabled: bool) -> None:
        self.setEnabled(enabled)
        self._opacity_effect.setOpacity(1.0 if enabled else 0.5)


class _SmallSwitch(QFrame):
    toggled = QtSignal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = checked
        self._pressed_inside = False
        self.setProperty("class", "ReaderSettingsSmallSwitch")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.settings_switch_width, Theme.settings_switch_height)

        self._knob = QFrame(self)
        self._knob.setProperty("class", "ReaderSettingsSmallSwitchKnob")
        self._knob.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._knob.setFixedSize(Theme.settings_switch_knob_size, Theme.settings_switch_knob_size)
        self._position_knob()

    @property
    def checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool, *, emit: bool = True) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._position_knob()
        if emit:
            self.toggled.emit(checked)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_knob()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
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
                self.set_checked(not self._checked)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)

    def _position_knob(self) -> None:
        # Figma uses a 1px stroke and 2px visual padding. Qt borders consume
        # space, so the knob starts 1px from the widget edge to preserve 2px.
        margin = Theme.settings_switch_layout_margin
        x = self.width() - margin - Theme.settings_switch_knob_size if self._checked else margin
        y = (self.height() - Theme.settings_switch_knob_size) // 2
        self._knob.move(x, y)


class _FitModeButton(QFrame):
    value_changed = QtSignal(object)

    _OPTIONS: tuple[tuple[str, ReaderFitMode], ...] = (
        ("Auto", ReaderFitMode.AUTO),
        ("Fit to Height", ReaderFitMode.FIT_HEIGHT),
        ("Fit to Width", ReaderFitMode.FIT_WIDTH),
        ("Fit to Page", ReaderFitMode.FIT_PAGE),
    )

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self._value = ReaderFitMode.AUTO
        self._pressed_inside = False
        self.setProperty("class", "ReaderSettingsSmallControl")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.reader_settings_control_width, Theme.reader_settings_option_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._label = QLabel("Auto")
        self._label.setProperty("class", "ReaderSettingsControlText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label, stretch=1)

        indicator = QLabel()
        indicator.setFixedSize(Theme.settings_dropdown_indicator_width, Theme.reader_settings_option_height)
        indicator.setPixmap(
            QIcon(str(resources.icon_path("icon_dropout.svg"))).pixmap(
                QSize(Theme.settings_dropdown_icon_size, Theme.settings_dropdown_icon_size)
            )
        )
        indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(indicator)

    @property
    def value(self) -> ReaderFitMode:
        return self._value

    def set_value(self, value: ReaderFitMode, *, emit: bool = True) -> None:
        if value == self._value:
            return
        self._value = value
        self._label.setText(_fit_mode_label(value))
        if emit:
            self.value_changed.emit(value)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
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
                self._show_menu()
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)

    def _show_menu(self) -> None:
        menu = FigmaMenu(self, width=self.width())
        for label, mode in self._OPTIONS:
            menu.add_item(label, lambda selected=mode: self.set_value(selected), selected=mode == self._value)
        menu.exec(self.mapToGlobal(QPoint(0, self.height())))


class _SpinButton(QFrame):
    value_changed = QtSignal(int)

    def __init__(
        self,
        resources: ResourceLoader,
        *,
        suffix: str,
        value: int,
        minimum: int,
        maximum: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._value = value
        self._minimum = minimum
        self._maximum = maximum
        self._suffix = suffix
        self.setProperty("class", "ReaderSettingsSmallControl")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.reader_settings_control_width, Theme.reader_settings_option_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(1, 0, 1, 0)
        layout.setSpacing(0)

        self._field = QLineEdit()
        self._field.setProperty("class", "ReaderSettingsValueField")
        self._field.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._field.returnPressed.connect(self.commit_text)
        layout.addWidget(self._field, stretch=1)

        layout.addWidget(_IconStepButton(resources, "icon_left.svg", lambda: self.step_by(-1)))
        layout.addWidget(_IconStepButton(resources, "icon_right.svg", lambda: self.step_by(1)))
        self._refresh_label()

    @property
    def value(self) -> int:
        return self._value

    def set_value(self, value: int, *, emit: bool = True) -> None:
        value = max(self._minimum, min(self._maximum, int(value)))
        if value == self._value:
            self._refresh_label()
            return
        self._value = value
        self._refresh_label()
        if emit:
            self.value_changed.emit(value)

    def step_by(self, delta: int) -> None:
        self.set_value(self._value + delta)

    def commit_text(self) -> None:
        value = _parse_numeric_text(self._field.text())
        if value is None:
            self._refresh_label()
            return
        self.set_value(value)

    def _refresh_label(self) -> None:
        self._field.setText(f"{self._value}{self._suffix}")


class _ZoomButton(QFrame):
    value_changed = QtSignal(int)

    def __init__(self, *, value: int, minimum: int, maximum: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "ReaderSettingsSmallControl")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.reader_settings_control_width, Theme.reader_settings_option_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(1, 0, 1, 0)
        layout.setSpacing(0)

        self._field = QLineEdit()
        self._field.setProperty("class", "ReaderSettingsValueField")
        self._field.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._field.setFixedWidth(Theme.reader_settings_zoom_label_width)
        self._field.setTextMargins(0, 0, 0, 0)
        self._field.returnPressed.connect(self.commit_text)
        layout.addWidget(self._field)

        self._slider = _MiniSlider(minimum=minimum, maximum=maximum, value=value)
        self._slider.valueChanged.connect(self.set_value)
        layout.addWidget(self._slider, stretch=1)
        self.set_value(value, emit=False)

    @property
    def value(self) -> int:
        return self._slider.value()

    def set_value(self, value: int, *, emit: bool = True) -> None:
        value = max(self._slider.minimum(), min(self._slider.maximum(), int(value)))
        if self._slider.value() != value:
            self._slider.blockSignals(True)
            self._slider.setValue(value)
            self._slider.blockSignals(False)
        self._field.setText(f"{value} %")
        if emit:
            self.value_changed.emit(value)

    def commit_text(self) -> None:
        value = _parse_numeric_text(self._field.text())
        if value is None:
            self._field.setText(f"{self._slider.value()} %")
            return
        self.set_value(value)


class _MiniSlider(QSlider):
    def __init__(self, *, minimum: int, maximum: int, value: int, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName("ReaderSettingsMiniSlider")
        self.setMinimum(minimum)
        self.setMaximum(maximum)
        self.setValue(value)
        self.setFixedHeight(Theme.reader_settings_option_height)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        track = QRectF(
            2,
            (self.height() - Theme.reader_settings_zoom_track_height) / 2,
            max(0, self.width() - 4),
            Theme.reader_settings_zoom_track_height,
        )
        radius = Theme.reader_settings_zoom_track_height / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*Theme.color_reader_slider_empty_rgba))
        painter.drawRoundedRect(track, radius, radius)

        x = self._handle_center_x(track)
        filled = QRectF(track.left(), track.top(), max(0, x - track.left()), track.height())
        painter.setBrush(QColor(*Theme.color_reader_slider_filled_rgba))
        painter.drawRoundedRect(filled, radius, radius)

        handle = QRectF(
            x - (Theme.reader_settings_zoom_knob_width / 2),
            (self.height() - Theme.reader_settings_zoom_knob_height) / 2,
            Theme.reader_settings_zoom_knob_width,
            Theme.reader_settings_zoom_knob_height,
        )
        painter.setBrush(QColor(Theme.color_window))
        painter.setPen(QPen(QColor(Theme.color_button_inner_edge), 1))
        painter.drawRoundedRect(handle, Theme.reader_settings_zoom_knob_height / 2, Theme.reader_settings_zoom_knob_height / 2)
        painter.end()

    def _handle_center_x(self, track: QRectF) -> float:
        span = self.maximum() - self.minimum()
        fraction = 0.0 if span <= 0 else (self.value() - self.minimum()) / span
        return track.left() + track.width() * max(0.0, min(1.0, fraction))


class _IconStepButton(QToolButton):
    def __init__(
        self,
        resources: ResourceLoader,
        icon_name: str,
        callback: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("class", "ReaderSettingsStepButton")
        self.setIcon(QIcon(str(resources.icon_path(icon_name))))
        self.setIconSize(QSize(Theme.reader_settings_step_icon_size, Theme.reader_settings_step_icon_size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.reader_settings_step_button_width, Theme.reader_settings_option_height)
        self.clicked.connect(callback)


def _fit_mode_label(mode: ReaderFitMode) -> str:
    return {
        ReaderFitMode.AUTO: "Auto",
        ReaderFitMode.FIT_HEIGHT: "Fit to Height",
        ReaderFitMode.FIT_WIDTH: "Fit to Width",
        ReaderFitMode.FIT_PAGE: "Fit to Page",
    }.get(mode, "Auto")


def _parse_numeric_text(text: str) -> int | None:
    match = re.fullmatch(r"\s*(-?\d+)\s*(?:px|%)?\s*", text, flags=re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


def _right_rounded_path(bounds: QRectF) -> QPainterPath:
    radius = max(0.0, min(float(Theme.reader_radius), bounds.width() / 2, bounds.height() / 2))
    path = QPainterPath()
    path.moveTo(bounds.left(), bounds.top())
    path.lineTo(bounds.right() - radius, bounds.top())
    path.quadTo(bounds.right(), bounds.top(), bounds.right(), bounds.top() + radius)
    path.lineTo(bounds.right(), bounds.bottom() - radius)
    path.quadTo(bounds.right(), bounds.bottom(), bounds.right() - radius, bounds.bottom())
    path.lineTo(bounds.left(), bounds.bottom())
    path.closeSubpath()
    return path
