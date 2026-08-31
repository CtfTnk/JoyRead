"""Custom title bar matching the Figma window banner."""

from __future__ import annotations

import sys
from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import SortField, ViewMode
from joyread.ui.widgets.dropdown_button import FigmaDropdownButton
from joyread.ui.widgets.menus import FigmaMenu
from joyread.ui.widgets.mode_switches import ListModeSwitchWidget, SortModeSwitchWidget
from joyread.ui.widgets.window_gestures import SystemMoveGesture
from joyread.ui.widgets.window_state import toggle_maximized


class TitleBarWidget(QWidget):
    sidebar_toggle_requested = QtSignal()
    sort_changed = QtSignal(str, bool)
    view_mode_changed = QtSignal(str)

    def __init__(
        self,
        resources: ResourceLoader,
        parent: QWidget | None = None,
        *,
        platform_name: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._resources = resources
        self._platform_name = platform_name or sys.platform
        self._sort_ascending = False
        self._current_sort_field: SortField = SortField.ADD_TIME
        self._drag = SystemMoveGesture()

        self.setObjectName("TitleBar")
        self.setFixedHeight(Theme.toolbar_height)
        self.setMouseTracking(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 8, 8)
        layout.setSpacing(10)

        self._stoplight_controls = StoplightControlsWidget()
        self._stoplight_controls.close_requested.connect(lambda: self.window().close())
        self._stoplight_controls.minimize_requested.connect(lambda: self.window().showMinimized())
        self._stoplight_controls.zoom_requested.connect(self._toggle_zoom)
        layout.addWidget(self._stoplight_controls)

        self._sidebar_button = _chrome_button(
            icon=self._icon("icon_sidebar.svg"),
            size=(36, 36),
            tooltip=t("toolbar.sidebar_toggle"),
        )
        self._sidebar_button.setCheckable(True)
        self._sidebar_button.setChecked(True)
        self._sidebar_button.clicked.connect(self.sidebar_toggle_requested.emit)
        layout.addWidget(self._sidebar_button)

        self._action_button = ActionMenuButton(resources)
        layout.addWidget(self._action_button)

        layout.addStretch(1)

        self._list_mode_switch = ListModeSwitchWidget(resources)
        self._list_mode_switch.value_changed.connect(self.view_mode_changed.emit)
        layout.addWidget(self._list_mode_switch)

        self._toolbar_spacer = _spacer()
        layout.addWidget(self._toolbar_spacer)

        self._sort_dropdown = FigmaDropdownButton(
            resources,
            [_sort_label(f) for f in SortField],
            width=Theme.sort_dropdown_width,
            initial_value=_sort_label(SortField.ADD_TIME),
            tooltip=t("toolbar.sort_by"),
        )
        self._sort_dropdown.value_changed.connect(self._emit_sort)
        layout.addWidget(self._sort_dropdown)

        self._sort_mode_switch = SortModeSwitchWidget(resources)
        self._sort_mode_switch.value_changed.connect(self._handle_sort_mode_changed)
        layout.addWidget(self._sort_mode_switch)

        self._title_control_group = TitleControlGroup(resources)
        self._title_control_group.close_requested.connect(lambda: self.window().close())
        self._title_control_group.minimize_requested.connect(lambda: self.window().showMinimized())
        self._title_control_group.zoom_requested.connect(self._toggle_zoom)
        layout.addWidget(self._title_control_group, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._shelf_controls = (
            self._action_button,
            self._list_mode_switch,
            self._toolbar_spacer,
            self._sort_dropdown,
            self._sort_mode_switch,
        )
        self.set_title_control_mode(
            force_non_macos_title_controls=False,
        )

    def set_action_menu_factory(self, build_menu: Callable[[], FigmaMenu]) -> None:
        self._action_button.set_menu_factory(build_menu)

    def set_sidebar_visible(self, visible: bool) -> None:
        self._sidebar_button.setChecked(visible)

    def set_view_mode(self, mode: str) -> None:
        self._list_mode_switch.set_value(mode)

    def set_sort(self, field: str, ascending: bool) -> None:
        sort_field = _sort_field_from_value(field)
        self._current_sort_field = sort_field
        self._sort_dropdown.set_value(_sort_label(sort_field))
        self._sort_ascending = ascending
        self._sort_mode_switch.set_ascending(ascending)

    def refresh_sort_labels(self) -> None:
        """Rebuild the sort dropdown labels after a locale change."""
        new_options = [_sort_label(f) for f in SortField]
        new_value = _sort_label(self._current_sort_field)
        self._sort_dropdown.update_options(new_options, new_value)

    def refresh_labels(self) -> None:
        """Refresh translated tooltips and display labels after a locale change."""
        self._sidebar_button.setToolTip(t("toolbar.sidebar_toggle"))
        self._sort_dropdown.setToolTip(t("toolbar.sort_by"))
        self._action_button.refresh_tooltip()
        self._stoplight_controls.refresh_tooltips()
        self._title_control_group.refresh_tooltips()
        self.refresh_sort_labels()

    def set_shelf_controls_visible(self, visible: bool) -> None:
        for control in self._shelf_controls:
            control.setVisible(visible)

    def set_title_control_mode(
        self,
        *,
        force_non_macos_title_controls: bool,
    ) -> None:
        show_non_macos_controls = force_non_macos_title_controls or self._platform_name != "darwin"
        show_stoplights = self._platform_name == "darwin" and not show_non_macos_controls
        self._stoplight_controls.setVisible(show_stoplights)
        self._title_control_group.setVisible(show_non_macos_controls)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag.press(self, event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag.move(self, event):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag.release()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_zoom()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _handle_sort_mode_changed(self, value: str) -> None:
        self._sort_ascending = value == SortModeSwitchWidget.ASCENDING
        self._emit_sort(self._sort_dropdown.value)

    def _emit_sort(self, label: str) -> None:
        # Dropdown emits the translated display label; convert to canonical
        # SortField value before propagating so ShelfViewModel stays locale-agnostic.
        field = _sort_field_from_label(label)
        self._current_sort_field = field
        self.sort_changed.emit(field.value, self._sort_ascending)

    def _toggle_zoom(self) -> None:
        toggle_maximized(self.window())

    def _icon(self, name: str) -> QIcon:
        return QIcon(str(self._resources.icon_path(name)))


class StoplightControlsWidget(QWidget):
    close_requested = QtSignal()
    minimize_requested = QtSignal()
    zoom_requested = QtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(Theme.traffic_light_group_width, Theme.traffic_light_group_height)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        # layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        self.close_button = _traffic_button("CloseButton", t("toolbar.close"))
        self.minimize_button = _traffic_button("MinimizeButton", t("toolbar.minimize"))
        self.zoom_button = _traffic_button("ZoomButton", t("toolbar.maximize"))
        self.close_button.clicked.connect(self.close_requested.emit)
        self.minimize_button.clicked.connect(self.minimize_requested.emit)
        self.zoom_button.clicked.connect(self.zoom_requested.emit)

        layout.addWidget(self.close_button)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.zoom_button)

    def refresh_tooltips(self) -> None:
        self.close_button.setToolTip(t("toolbar.close"))
        self.minimize_button.setToolTip(t("toolbar.minimize"))
        self.zoom_button.setToolTip(t("toolbar.maximize"))


class TitleControlGroup(QWidget):
    close_requested = QtSignal()
    minimize_requested = QtSignal()
    zoom_requested = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleControlGroup")
        self.setFixedSize(Theme.title_control_group_width, Theme.title_control_group_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, Theme.title_control_group_right_inset, 0)
        layout.setSpacing(Theme.title_control_gap)

        self.minimize_button = TitleControlButton(
            QIcon(str(resources.icon_path("icon_frame_minimise.svg"))),
            t("toolbar.minimize"),
            "TitleControlMinimizeButton",
        )
        self.zoom_button = TitleControlButton(
            QIcon(str(resources.icon_path("icon_frame_expand.svg"))),
            t("toolbar.maximize"),
            "TitleControlZoomButton",
        )
        self.close_button = TitleControlButton(
            QIcon(str(resources.icon_path("icon_frame_close.svg"))),
            t("toolbar.close"),
            "TitleControlCloseButton",
        )
        self.minimize_button.clicked.connect(self.minimize_requested.emit)
        self.zoom_button.clicked.connect(self.zoom_requested.emit)
        self.close_button.clicked.connect(self.close_requested.emit)

        layout.addWidget(self.minimize_button)
        layout.addWidget(self.zoom_button)
        layout.addWidget(self.close_button)

    def refresh_tooltips(self) -> None:
        self.minimize_button.setToolTip(t("toolbar.minimize"))
        self.zoom_button.setToolTip(t("toolbar.maximize"))
        self.close_button.setToolTip(t("toolbar.close"))


class TitleControlButton(QToolButton):
    """Paints the Figma 24px window-control buttons without QSS border sizing."""

    def __init__(self, icon: QIcon, tooltip: str, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setIcon(icon)
        self.setIconSize(QSize(Theme.title_control_button_size, Theme.title_control_button_size))
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.title_control_button_size, Theme.title_control_button_size)

    def enterEvent(self, event) -> None:  # noqa: ANN001 - PySide event type differs by Qt minor version.
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - PySide event type differs by Qt minor version.
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        background = QColor(Theme.color_selected if self.underMouse() or self.isDown() else Theme.color_window)
        stroke_width = Theme.title_control_stroke_width
        inset = stroke_width / 2
        frame_rect = QRectF(
            inset,
            inset,
            self.width() - stroke_width,
            self.height() - stroke_width,
        )
        painter.setBrush(background)
        painter.setPen(QPen(QColor(Theme.color_button_edge), stroke_width))
        painter.drawRoundedRect(
            frame_rect,
            Theme.title_control_button_radius,
            Theme.title_control_button_radius,
        )

        icon_rect = QRect(0, 0, Theme.title_control_button_size, Theme.title_control_button_size)
        self.icon().paint(painter, icon_rect)


class ActionMenuButton(QFrame):
    """Figma button_action: 42x36, left 24px icon, right 10px dropout indicator."""

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_menu: Callable[[], FigmaMenu] | None = None
        self.setProperty("class", "ActionMenuButton")
        self.setToolTip(t("toolbar.actions"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.action_button_width, Theme.action_button_height)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(4)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 64))
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 3, 4)
        layout.setSpacing(1)

        action_icon = QLabel()
        action_icon.setFixedSize(Theme.icon_size, Theme.icon_size)
        action_icon.setPixmap(QIcon(str(resources.icon_path("icon_action.svg"))).pixmap(_icon_qsize()))
        layout.addWidget(action_icon, alignment=Qt.AlignmentFlag.AlignVCenter)

        layout.addStretch(1)

        dropout_icon = QLabel()
        dropout_icon.setFixedSize(Theme.action_button_indicator_size, Theme.action_button_indicator_size)
        dropout_icon.setPixmap(
            QIcon(str(resources.icon_path("icon_dropout.svg"))).pixmap(
                QSize(Theme.action_button_indicator_size, Theme.action_button_indicator_size)
            )
        )
        layout.addWidget(dropout_icon, alignment=Qt.AlignmentFlag.AlignVCenter)

    def set_menu_factory(self, build_menu: Callable[[], FigmaMenu]) -> None:
        # A factory rather than a menu: FigmaMenu.exec() destroys the menu it
        # showed, so every press needs its own. Building on demand also picks
        # up the current locale for the row labels.
        self._build_menu = build_menu

    def refresh_tooltip(self) -> None:
        self.setToolTip(t("toolbar.actions"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._build_menu is not None:
            menu = self._build_menu()
            menu.closed.connect(self._finish_menu_interaction)
            self._finish_menu_interaction()
            menu.exec(self.mapToGlobal(QPoint(0, self.height())))
            event.accept()
            return
        super().mousePressEvent(event)

    def _finish_menu_interaction(self) -> None:
        # Qt popup windows can consume the leave event that would normally
        # reset QSS :hover on the trigger frame.
        self.clearFocus()
        app = QApplication.instance()
        if app is not None:
            app.sendEvent(self, QEvent(QEvent.Type.Leave))
        self.update()


def _sort_label(field: SortField) -> str:
    """Return the translated display label for a SortField (e.g. SortField.ADD_TIME → "Add Time")."""
    return t(f"sort.{field.name.lower()}")


def _sort_field_from_label(label: str) -> SortField:
    """Reverse-map a translated display label back to its SortField enum member."""
    for field in SortField:
        if _sort_label(field) == label:
            return field
    return SortField.ADD_TIME  # safe fallback


def _sort_field_from_value(value: str) -> SortField:
    """Convert a canonical SortField value string (e.g. 'Add Time') to its enum member."""
    try:
        return SortField(value)
    except ValueError:
        return SortField.ADD_TIME


def _traffic_button(object_name: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName(object_name)
    button.setToolTip(tooltip)
    button.setFixedSize(Theme.traffic_light_size, Theme.traffic_light_size)

    # gpt fix
    # button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    return button


def _chrome_button(icon: QIcon, size: tuple[int, int], tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setProperty("class", "ChromeButton")
    button.setIcon(icon)
    button.setIconSize(_icon_qsize())
    button.setToolTip(tooltip)
    button.setFixedSize(*size)
    return button


def _spacer() -> QFrame:
    frame = QFrame()
    frame.setObjectName("ToolbarSpacer")
    frame.setFixedSize(Theme.toolbar_spacer_width, Theme.toolbar_control_height)
    frame.setFrameShape(QFrame.Shape.NoFrame)
    return frame


def _icon_qsize() -> QSize:
    return QSize(Theme.icon_size, Theme.icon_size)


WindowChromeWidget = TitleBarWidget
WindowControlsWidget = StoplightControlsWidget
