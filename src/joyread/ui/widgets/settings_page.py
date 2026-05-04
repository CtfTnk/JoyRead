"""Reusable settings page widgets adapted from Figma node 231:1738."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsSectionKey, SettingsViewModel
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.menus import FigmaMenu


class SettingsPageWidget(QFrame):
    storage_change_requested = QtSignal()

    def __init__(
        self,
        viewmodel: SettingsViewModel,
        resources: ResourceLoader,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._resources = resources
        self._sidebar_items: dict[SettingsSectionKey, SettingsSidebarItem] = {}
        self.setProperty("class", "SettingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(Theme.settings_panel_min_width, Theme.settings_panel_min_height)
        self.setMaximumSize(Theme.settings_panel_max_width, Theme.settings_panel_max_height)
        self.resize(Theme.settings_panel_width, Theme.settings_panel_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QHBoxLayout(self)
        # Figma gives the outer panel 10px visual padding and a 2px stroke.
        # Qt's border consumes layout space, so 8px margins preserve the inset.
        layout.setContentsMargins(
            Theme.settings_panel_layout_margin,
            Theme.settings_panel_layout_margin,
            Theme.settings_panel_layout_margin,
            Theme.settings_panel_layout_margin,
        )
        layout.setSpacing(Theme.settings_panel_gap)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._sidebar = SettingsSidebarWidget(viewmodel, self)
        layout.addWidget(self._sidebar)

        self._content = SettingsContentPanel()
        layout.addWidget(self._content, stretch=1)

        self._viewmodel.state_changed.connect(self.render)
        self.render()

    def sizeHint(self) -> QSize:
        return QSize(Theme.settings_panel_width, Theme.settings_panel_height)

    def render(self) -> None:
        self._sidebar.set_active(self._viewmodel.current_section)
        self._content.set_items(self._items_for_current_section())

    def _items_for_current_section(self) -> list[QWidget]:
        if self._viewmodel.current_section == SettingsSectionKey.GENERAL:
            return self._general_items()
        return []

    def _general_items(self) -> list[QWidget]:
        language = SettingsDropdownItem(
            "Language",
            self._viewmodel.language,
            ("English",),
            self._resources,
        )
        language.value_changed.connect(self._viewmodel.set_language)

        import_switch = SettingsSwitchItem(
            "Import book when opening",
            self._viewmodel.import_book_when_opening,
        )
        import_switch.toggled.connect(self._viewmodel.set_import_book_when_opening)

        window_switch = SettingsSwitchItem(
            "Individual Read Window",
            self._viewmodel.individual_read_window,
        )
        window_switch.toggled.connect(self._viewmodel.set_individual_read_window)

        storage = SettingsAddressItem("Storage Location", self._viewmodel.storage_location)
        storage.change_requested.connect(self.storage_change_requested.emit)

        return [language, import_switch, window_switch, storage]


class SettingsSidebarWidget(QFrame):
    def __init__(self, viewmodel: SettingsViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._items: dict[SettingsSectionKey, SettingsSidebarItem] = {}
        self.setObjectName("SettingsSidebar")
        self.setFixedWidth(Theme.settings_sidebar_width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            Theme.settings_sidebar_layout_margin,
            Theme.settings_sidebar_layout_margin,
            Theme.settings_sidebar_layout_margin,
            Theme.settings_sidebar_layout_margin,
        )
        root_layout.setSpacing(0)

        upper_part = QWidget()
        upper_part.setObjectName("SettingsSidebarUpperPart")
        upper_layout = QVBoxLayout(upper_part)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(Theme.settings_sidebar_gap)
        upper_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        lower_part = QWidget()
        lower_part.setObjectName("SettingsSidebarLowerPart")
        lower_layout = QVBoxLayout(lower_part)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.setSpacing(0)
        lower_layout.setAlignment(Qt.AlignmentFlag.AlignBottom)

        for section in viewmodel.sections:
            item = SettingsSidebarItem(section.label, section.key)
            item.clicked.connect(viewmodel.set_section)
            self._items[section.key] = item
            if section.lower_group:
                lower_layout.addWidget(item)
            else:
                upper_layout.addWidget(item)

        root_layout.addWidget(upper_part, stretch=1)
        root_layout.addWidget(lower_part)

    def set_active(self, key: SettingsSectionKey) -> None:
        for section_key, item in self._items.items():
            item.set_checked(section_key == key)


class SettingsSidebarItem(QFrame):
    clicked = QtSignal(str)

    def __init__(self, label: str, key: SettingsSectionKey, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._pressed_inside = False
        self.setProperty("class", "SettingsSidebarItem")
        self.setProperty("selected", "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.settings_sidebar_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.settings_sidebar_item_padding_left,
            Theme.settings_sidebar_item_padding_vertical,
            Theme.settings_sidebar_item_padding_right,
            Theme.settings_sidebar_item_padding_vertical,
        )
        layout.setSpacing(0)

        text = QLabel(label)
        text.setProperty("class", "SettingsSidebarItemText")
        text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(text)
        layout.addStretch(1)

    def set_checked(self, checked: bool) -> None:
        self.setProperty("selected", "true" if checked else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

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
                self.clicked.emit(self._key.value)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)


class SettingsContentPanel(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsRightScrollArea")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setObjectName("SettingsRightViewport")
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._content = QWidget()
        self._content.setObjectName("SettingsRightContent")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(
            Theme.settings_content_padding,
            Theme.settings_content_padding,
            Theme.settings_content_padding,
            Theme.settings_content_padding,
        )
        self._layout.setSpacing(Theme.settings_content_gap)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)
        self._scroll_handle = AutoHideScrollHandle(self)

    def set_items(self, items: Iterable[QWidget]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for item in items:
            self._layout.addWidget(item)
        self._layout.addStretch(1)


class SettingsOptionItem(QFrame):
    def __init__(self, name: str, option: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "SettingsItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(Theme.settings_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.settings_item_padding,
            Theme.settings_item_padding,
            Theme.settings_item_padding,
            Theme.settings_item_padding,
        )
        layout.setSpacing(0)

        layout.addWidget(_SettingsNameCell(name), stretch=1)
        option_frame = QWidget()
        option_frame.setObjectName("SettingsItemOption")
        option_frame.setFixedHeight(Theme.settings_item_name_height)
        option_layout = QHBoxLayout(option_frame)
        option_layout.setContentsMargins(0, 0, 0, 0)
        option_layout.setSpacing(0)
        option_layout.addWidget(option)
        layout.addWidget(option_frame)


class SettingsDropdownItem(SettingsOptionItem):
    value_changed = QtSignal(str)

    def __init__(
        self,
        name: str,
        value: str,
        options: tuple[str, ...],
        resources: ResourceLoader,
        parent: QWidget | None = None,
    ) -> None:
        self.dropdown = SettingsDropdownButton(value, options, resources)
        super().__init__(name, self.dropdown, parent)
        self.dropdown.value_changed.connect(self.value_changed.emit)


class SettingsSwitchItem(SettingsOptionItem):
    toggled = QtSignal(bool)

    def __init__(self, name: str, checked: bool, parent: QWidget | None = None) -> None:
        self.switch = SettingsSwitchControl(checked)
        option = QWidget()
        option.setObjectName("SettingsSwitchOption")
        option_layout = QHBoxLayout(option)
        option_layout.setContentsMargins(
            Theme.settings_switch_option_padding_horizontal,
            0,
            Theme.settings_switch_option_padding_horizontal,
            0,
        )
        option_layout.setSpacing(0)
        option_layout.addWidget(self.switch)
        super().__init__(name, option, parent)
        self.switch.toggled.connect(self.toggled.emit)


class SettingsAddressItem(QFrame):
    change_requested = QtSignal()

    def __init__(self, title: str, directory: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "SettingsItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(Theme.settings_address_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.settings_item_padding,
            Theme.settings_item_padding,
            Theme.settings_item_padding,
            Theme.settings_item_padding,
        )
        layout.setSpacing(Theme.settings_item_padding)

        layout.addWidget(_SettingsNameCell(title))

        option = QWidget()
        option.setObjectName("SettingsAddressOption")
        option.setFixedHeight(Theme.settings_address_option_height)
        option_layout = QHBoxLayout(option)
        option_layout.setContentsMargins(
            Theme.settings_address_option_padding_left,
            Theme.settings_item_padding,
            Theme.settings_address_option_padding_right,
            Theme.settings_item_padding,
        )
        option_layout.setSpacing(Theme.settings_address_option_gap)

        path = QLineEdit(directory)
        path.setProperty("class", "SettingsPathField")
        path.setReadOnly(True)
        path.setFixedHeight(Theme.settings_path_height)
        path.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        option_layout.addWidget(path, stretch=1)

        change_button = SettingsPushButton("Change")
        change_button.clicked.connect(self.change_requested.emit)
        option_layout.addWidget(change_button)

        layout.addWidget(option)


class SettingsDropdownButton(QFrame):
    value_changed = QtSignal(str)

    def __init__(
        self,
        value: str,
        options: tuple[str, ...],
        resources: ResourceLoader,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._options = options
        self._value = value
        self._resources = resources
        self._pressed_inside = False
        self.setProperty("class", "SettingsDropdownButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.settings_dropdown_width, Theme.settings_dropdown_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._label = QLabel(value)
        self._label.setProperty("class", "SettingsControlText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label, stretch=1)

        icon_frame = QWidget()
        icon_frame.setObjectName("SettingsDropdownIndicator")
        icon_frame.setFixedWidth(Theme.settings_dropdown_indicator_width)
        icon_layout = QHBoxLayout(icon_frame)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon_layout.setSpacing(0)
        icon = QLabel()
        icon.setFixedSize(Theme.settings_dropdown_icon_size, Theme.settings_dropdown_icon_size)
        icon.setPixmap(
            QIcon(str(resources.icon_path("icon_dropout.svg"))).pixmap(
                QSize(Theme.settings_dropdown_icon_size, Theme.settings_dropdown_icon_size)
            )
        )
        icon_layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_frame)

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        if value == self._value:
            return
        self._value = value
        self._label.setText(value)
        self.value_changed.emit(value)

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
                self._show_menu()
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)

    def _show_menu(self) -> None:
        menu = FigmaMenu(self, width=self.width())
        for option in self._options:
            menu.add_item(option, lambda selected=option: self.set_value(selected))
        menu.exec(self.mapToGlobal(QPoint(0, self.height())))


class SettingsPushButton(QToolButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "SettingsPushButton")
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.settings_push_button_width, Theme.settings_push_button_height)


class SettingsSwitchControl(QFrame):
    toggled = QtSignal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = checked
        self._pressed_inside = False
        self.setProperty("class", "SettingsSwitch")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.settings_switch_width, Theme.settings_switch_height)

        self._knob = QFrame(self)
        self._knob.setProperty("class", "SettingsSwitchKnob")
        self._knob.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._knob.setFixedSize(Theme.settings_switch_knob_size, Theme.settings_switch_knob_size)
        self._position_knob()

    @property
    def checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._position_knob()
        self.toggled.emit(checked)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_knob()

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
                self.set_checked(not self._checked)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)

    def _position_knob(self) -> None:
        margin = Theme.settings_switch_layout_margin
        x = self.width() - margin - Theme.settings_switch_knob_size if self._checked else margin
        y = (self.height() - Theme.settings_switch_knob_size) // 2
        self._knob.move(x, y)


class _SettingsNameCell(QWidget):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsItemName")
        self.setFixedHeight(Theme.settings_item_name_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.settings_item_name_padding,
            0,
            Theme.settings_item_name_padding,
            0,
        )
        layout.setSpacing(0)

        label = QLabel(text)
        label.setProperty("class", "SettingsItemNameText")
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(label)
