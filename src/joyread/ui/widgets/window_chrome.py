"""Custom frameless title bar matching the Figma window banner."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import SortField, ViewMode
from joyread.ui.widgets.dropdown_button import FigmaDropdownButton
from joyread.ui.widgets.menus import FigmaMenu
from joyread.ui.widgets.mode_switches import ListModeSwitchWidget, SortModeSwitchWidget


class WindowChromeWidget(QWidget):
    sidebar_toggle_requested = QtSignal()
    sort_changed = QtSignal(str, bool)
    view_mode_changed = QtSignal(str)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self._sort_ascending = False
        self._drag_position: QPoint | None = None

        self.setObjectName("WindowChrome")
        self.setFixedHeight(Theme.toolbar_height)
        self.setMouseTracking(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 8, 8)
        layout.setSpacing(10)

        controls = WindowControlsWidget()
        controls.close_requested.connect(lambda: self.window().close())
        controls.minimize_requested.connect(lambda: self.window().showMinimized())
        controls.zoom_requested.connect(self._toggle_zoom)
        layout.addWidget(controls)

        self._sidebar_button = _chrome_button(
            icon=self._icon("icon_sidebar.svg"),
            size=(36, 36),
            tooltip="Toggle sidebar",
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
            [field.value for field in SortField],
            width=Theme.sort_dropdown_width,
            initial_value=SortField.ADD_TIME.value,
            tooltip="Sort by",
        )
        self._sort_dropdown.value_changed.connect(self._emit_sort)
        layout.addWidget(self._sort_dropdown)

        self._sort_mode_switch = SortModeSwitchWidget(resources)
        self._sort_mode_switch.value_changed.connect(self._handle_sort_mode_changed)
        layout.addWidget(self._sort_mode_switch)
        self._shelf_controls = (
            self._action_button,
            self._list_mode_switch,
            self._toolbar_spacer,
            self._sort_dropdown,
            self._sort_mode_switch,
        )

    def set_action_menu(self, menu: FigmaMenu) -> None:
        self._action_button.setMenu(menu)

    def set_sidebar_visible(self, visible: bool) -> None:
        self._sidebar_button.setChecked(visible)

    def set_view_mode(self, mode: str) -> None:
        self._list_mode_switch.set_value(mode)

    def set_sort(self, field: str, ascending: bool) -> None:
        self._sort_dropdown.set_value(field)
        self._sort_ascending = ascending
        self._sort_mode_switch.set_ascending(ascending)

    def set_shelf_controls_visible(self, visible: bool) -> None:
        for control in self._shelf_controls:
            control.setVisible(visible)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_position = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self.window().isMaximized():
                return
            self.window().move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_position = None
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

    def _emit_sort(self, field: str) -> None:
        self.sort_changed.emit(field, self._sort_ascending)

    def _toggle_zoom(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    def _icon(self, name: str) -> QIcon:
        return QIcon(str(self._resources.icon_path(name)))


class WindowControlsWidget(QWidget):
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

        close_button = _traffic_button("CloseButton", "Close")
        minimize_button = _traffic_button("MinimizeButton", "Minimize")
        zoom_button = _traffic_button("ZoomButton", "Zoom")
        close_button.clicked.connect(self.close_requested.emit)
        minimize_button.clicked.connect(self.minimize_requested.emit)
        zoom_button.clicked.connect(self.zoom_requested.emit)

        layout.addWidget(close_button)
        layout.addWidget(minimize_button)
        layout.addWidget(zoom_button)


class ActionMenuButton(QFrame):
    """Figma button_action: 42x36, left 24px icon, right 10px dropout indicator."""

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._menu: FigmaMenu | None = None
        self.setProperty("class", "ActionMenuButton")
        self.setToolTip("Actions")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.action_button_width, Theme.action_button_height)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(4)
        shadow.setOffset(0, 0)
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

    def setMenu(self, menu: FigmaMenu) -> None:
        self._menu = menu
        self._menu.closed.connect(self._finish_menu_interaction)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._menu is not None:
            self._finish_menu_interaction()
            self._menu.exec(self.mapToGlobal(QPoint(0, self.height())))
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
