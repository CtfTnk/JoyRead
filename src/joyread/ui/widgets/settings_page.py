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
from joyread.ui.viewmodels.settings_viewmodel import (
    ARCHIVE_POOL_MAX_MB,
    ARCHIVE_POOL_MIN_MB,
    ARCHIVE_CACHE_STRATEGY_OPTIONS,
    ARCHIVE_INTERNAL_DEPTH_MAX,
    ARCHIVE_INTERNAL_DEPTH_MIN,
    DETAIL_THUMBNAIL_CACHE_MAX_MB,
    DETAIL_THUMBNAIL_CACHE_MIN_MB,
    IMPORT_FOLDER_DEPTH_MAX,
    IMPORT_FOLDER_DEPTH_MIN,
    READER_PAGE_CACHE_MAX_MB,
    READER_PAGE_CACHE_MIN_MB,
    SettingsSectionKey,
    SettingsViewModel,
)
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.menus import FigmaMenu
from joyread.ui.widgets.section_banner import SectionBanner


class SettingsPageWidget(QFrame):
    storage_change_requested = QtSignal()
    # Hidden Space user-actions that need a dialog overlay. SettingsView
    # forwards them so MainWindow (which owns the overlay) can drive the
    # dialog flow and call the relevant VM methods.
    hidden_space_setup_requested = QtSignal()
    hidden_space_verify_requested = QtSignal()
    hidden_space_change_password_requested = QtSignal()
    hidden_space_revert_requested = QtSignal()
    hidden_space_reset_requested = QtSignal()

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
        if self._viewmodel.current_section == SettingsSectionKey.PRIVACY:
            return self._privacy_items()
        return []

    def _privacy_items(self) -> list[QWidget]:
        # Hidden Space surface. The switch row drives the dialog flow via
        # ``hidden_space_setup_requested`` / ``hidden_space_verify_requested``
        # — the VM only flips the persisted toggle off; turning it on
        # requires a password and is owned by MainWindow.
        hidden_banner = SectionBanner("Hidden Space", self._resources)

        show_switch = SettingsSwitchItem(
            "Show Collections",
            self._viewmodel.show_hidden_collection,
        )
        show_switch.toggled.connect(self._handle_show_hidden_toggled)

        change_password = SettingsButtonItem("Change Password", "Change")
        change_password.clicked.connect(self.hidden_space_change_password_requested.emit)
        # Hidden Space hasn't been set up yet → no password to change.
        change_password.set_enabled(self._viewmodel.hidden_space_initialized)

        revert = SettingsButtonItem("Revert all", "Proceed")
        revert.clicked.connect(self.hidden_space_revert_requested.emit)
        revert.set_enabled(self._viewmodel.hidden_space_initialized)

        reset = SettingsButtonItem("Reset and Erase", "Proceed", destructive=True)
        reset.clicked.connect(self.hidden_space_reset_requested.emit)
        reset.set_enabled(self._viewmodel.hidden_space_initialized)

        return [hidden_banner, show_switch, change_password, revert, reset]

    def _handle_show_hidden_toggled(self, enabled: bool) -> None:
        if enabled:
            # The switch flips visually before the dialog appears; we revert
            # it on failure inside the dialog handlers. Setup vs. verify is
            # decided by whether the feature has been initialised.
            if self._viewmodel.hidden_space_initialized:
                self.hidden_space_verify_requested.emit()
            else:
                self.hidden_space_setup_requested.emit()
        else:
            # Turning off the toggle is unprivileged — books stay marked
            # hidden in storage, just not displayed.
            self._viewmodel.set_show_hidden_collection(False)

    def revert_show_hidden_switch(self) -> None:
        # Re-render the Privacy items so the switch reflects the persisted
        # ``show_hidden_collection`` value again — used when the password
        # dialog is cancelled or verification fails.
        self.render()

    def _general_items(self) -> list[QWidget]:
        # General sub-group: existing settings, headed by the same banner
        # widget the sidebar uses for "Book Shelf" / "Collections" so the
        # grouping vocabulary is consistent across the app.
        general_banner = SectionBanner("General", self._resources)

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

        import_banner = SectionBanner("Import", self._resources)

        import_folder_depth_item = SettingsNumericItem(
            "Import folder depth",
            self._viewmodel.import_folder_max_depth,
            IMPORT_FOLDER_DEPTH_MIN,
            IMPORT_FOLDER_DEPTH_MAX,
            self._resources,
        )
        import_folder_depth_item.value_changed.connect(self._viewmodel.set_import_folder_max_depth)

        archive_depth_item = SettingsNumericItem(
            "Archive internal depth",
            self._viewmodel.archive_internal_max_depth,
            ARCHIVE_INTERNAL_DEPTH_MIN,
            ARCHIVE_INTERNAL_DEPTH_MAX,
            self._resources,
        )
        archive_depth_item.value_changed.connect(self._viewmodel.set_archive_internal_max_depth)

        # Cache sub-group: user-tunable cache budgets and a one-shot purge for
        # the disk pool. Live in General per design — there is no separate
        # "Performance" section.
        cache_banner = SectionBanner("Cache", self._resources)

        reader_cache_item = SettingsNumericItem(
            "Reader page cache (in-memory)",
            self._viewmodel.reader_page_cache_mb,
            READER_PAGE_CACHE_MIN_MB,
            READER_PAGE_CACHE_MAX_MB,
            self._resources,
            "MB",
        )
        reader_cache_item.value_changed.connect(self._viewmodel.set_reader_page_cache_mb)

        detail_cache_item = SettingsNumericItem(
            "Detail thumbnail cache (in-memory)",
            self._viewmodel.detail_thumbnail_cache_mb,
            DETAIL_THUMBNAIL_CACHE_MIN_MB,
            DETAIL_THUMBNAIL_CACHE_MAX_MB,
            self._resources,
            "MB",
        )
        detail_cache_item.value_changed.connect(self._viewmodel.set_detail_thumbnail_cache_mb)

        archive_pool_item = SettingsNumericItem(
            "Archive extraction pool (disk)",
            self._viewmodel.archive_extraction_pool_mb,
            ARCHIVE_POOL_MIN_MB,
            ARCHIVE_POOL_MAX_MB,
            self._resources,
            "MB",
        )
        archive_pool_item.value_changed.connect(self._viewmodel.set_archive_extraction_pool_mb)

        archive_strategy_item = SettingsDropdownItem(
            "Archive cache strategy",
            self._viewmodel.archive_cache_strategy_label,
            ARCHIVE_CACHE_STRATEGY_OPTIONS,
            self._resources,
        )
        archive_strategy_item.value_changed.connect(self._viewmodel.set_archive_cache_strategy)

        archive_pool_usage = SettingsCacheStatusItem(
            "Archive pool usage",
            current_bytes=self._viewmodel.archive_pool_current_bytes,
            budget_mb=self._viewmodel.archive_extraction_pool_mb,
        )
        archive_pool_usage.clear_requested.connect(self._viewmodel.request_clear_archive_pool)

        return [
            general_banner,
            language,
            import_switch,
            window_switch,
            storage,
            import_banner,
            import_folder_depth_item,
            archive_depth_item,
            cache_banner,
            reader_cache_item,
            detail_cache_item,
            archive_pool_item,
            archive_strategy_item,
            archive_pool_usage,
        ]


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


class SettingsButtonItem(SettingsOptionItem):
    """Label + right-aligned push button row (Figma node I231:1711;509:3236).

    Used for the Hidden Space rows (Change Password, Revert all, Reset and
    Erase). When ``destructive=True`` the label and button text are tinted
    red to match the Figma "Reset and Erase / Proceed" treatment.
    """

    clicked = QtSignal()

    def __init__(
        self,
        name: str,
        button_text: str,
        *,
        destructive: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        self.button = SettingsPushButton(button_text)
        if destructive:
            # Mirror the Figma `text-[#bf0c0c]` treatment on the destructive
            # variant. The QSS theme picks up ``destructive=true`` to swap
            # both the label and the button caption colour.
            self.button.setProperty("destructive", "true")
        super().__init__(name, self.button, parent)
        if destructive:
            self.setProperty("destructive", "true")
            self.style().unpolish(self)
            self.style().polish(self)
        self.button.clicked.connect(self.clicked.emit)

    def set_enabled(self, enabled: bool) -> None:
        self.button.setEnabled(enabled)


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


class SettingsNumericItem(SettingsOptionItem):
    """Label + Figma spin button row for cache numeric settings."""

    value_changed = QtSignal(int)

    def __init__(
        self,
        name: str,
        value: int,
        minimum: int,
        maximum: int,
        resources: ResourceLoader,
        suffix: str = "",
        parent: QWidget | None = None,
    ) -> None:
        self.spin_button = SettingsSpinButtonSmall(value, minimum, maximum, suffix, resources)
        super().__init__(name, self.spin_button, parent)
        self.spinbox = self.spin_button
        self.spin_button.value_changed.connect(self.value_changed.emit)


class SettingsSpinButtonSmall(QFrame):
    """Small numeric stepper adapted from Figma node 229:3566."""

    value_changed = QtSignal(int)

    def __init__(
        self,
        value: int,
        minimum: int,
        maximum: int,
        unit: str,
        resources: ResourceLoader,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._unit = unit
        self._value = max(self._minimum, min(self._maximum, int(value)))
        self.setProperty("class", "SettingsSpinButtonSmall")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.settings_spin_width, Theme.settings_spin_height)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.settings_spin_outer_padding,
            0,
            Theme.settings_spin_outer_padding,
            0,
        )
        layout.setSpacing(0)

        text = QWidget()
        text.setObjectName("SettingsSpinText")
        text_layout = QHBoxLayout(text)
        text_layout.setContentsMargins(
            Theme.settings_spin_text_padding,
            Theme.settings_spin_text_padding,
            Theme.settings_spin_text_padding,
            Theme.settings_spin_text_padding,
        )
        text_layout.setSpacing(Theme.settings_spin_text_gap)
        self._value_editor = QLineEdit()
        self._value_editor.setObjectName("SettingsSpinValueEditor")
        self._value_editor.setProperty("class", "SettingsSpinValueText")
        self._value_editor.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._value_editor.setFrame(False)
        self._value_editor.setMaxLength(max(len(str(self._minimum)), len(str(self._maximum))))
        self._value_editor.setFixedWidth(Theme.settings_spin_editor_width)
        self._value_editor.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        self._value_editor.returnPressed.connect(self._commit_editor_value)
        self._value_editor.editingFinished.connect(self._refresh_label)
        self._unit_label = QLabel(unit)
        self._unit_label.setProperty("class", "SettingsSpinValueText")
        self._unit_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        text_layout.addWidget(self._value_editor)
        if unit:
            text_layout.addWidget(self._unit_label)
        text_layout.addStretch(1)
        layout.addWidget(text, stretch=1)

        button_area = QWidget()
        button_area.setObjectName("SettingsSpinButtonArea")
        button_area.setFixedWidth(Theme.settings_spin_button_area_width)
        button_layout = QHBoxLayout(button_area)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(Theme.settings_spin_button_gap)
        button_layout.addWidget(self._step_button(resources, "icon_left.svg", -1))
        button_layout.addWidget(self._step_button(resources, "icon_right.svg", 1))
        layout.addWidget(button_area)
        self._refresh_label()

    @property
    def value(self) -> int:
        return self._value

    def set_value(self, value: int, *, emit: bool = True) -> None:
        clamped = max(self._minimum, min(self._maximum, int(value)))
        if clamped == self._value:
            self._refresh_label()
            return
        self._value = clamped
        self._refresh_label()
        if emit:
            self.value_changed.emit(clamped)

    def step_by(self, delta: int) -> None:
        self.set_value(self._value + delta)

    def _step_button(self, resources: ResourceLoader, icon_name: str, delta: int) -> QToolButton:
        button = QToolButton()
        button.setProperty("class", "SettingsSpinStepButton")
        button.setProperty("iconName", icon_name)
        button.setIcon(QIcon(str(resources.icon_path(icon_name))))
        button.setIconSize(QSize(Theme.settings_spin_icon_size, Theme.settings_spin_icon_size))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(Theme.settings_spin_step_button_width, Theme.settings_spin_height)
        button.setAutoRepeat(True)
        button.clicked.connect(lambda _checked=False, step=delta: self.step_by(step))
        return button

    def _refresh_label(self) -> None:
        self._value_editor.setText(str(self._value))

    def _commit_editor_value(self) -> None:
        try:
            value = int(self._value_editor.text())
        except ValueError:
            self._refresh_label()
            return
        self.set_value(value)


class SettingsCacheStatusItem(QFrame):
    """Row that displays current pool usage alongside a small ``Clear`` action."""

    clear_requested = QtSignal()

    def __init__(
        self,
        name: str,
        current_bytes: int,
        budget_mb: int,
        parent: QWidget | None = None,
    ) -> None:
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

        option = QWidget()
        option.setObjectName("SettingsItemOption")
        option.setFixedHeight(Theme.settings_item_name_height)
        option_layout = QHBoxLayout(option)
        option_layout.setContentsMargins(0, 0, Theme.settings_address_option_padding_right, 0)
        option_layout.setSpacing(Theme.settings_address_option_gap)

        self._usage_label = QLabel(_format_usage_label(current_bytes, budget_mb))
        self._usage_label.setObjectName("SettingsCacheUsageLabel")
        self._usage_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        option_layout.addWidget(self._usage_label, stretch=1)

        clear_button = SettingsPushButton("Clear")
        clear_button.clicked.connect(self.clear_requested.emit)
        option_layout.addWidget(clear_button)

        layout.addWidget(option)

    def set_usage(self, current_bytes: int, budget_mb: int) -> None:
        self._usage_label.setText(_format_usage_label(current_bytes, budget_mb))


def _format_usage_label(current_bytes: int, budget_mb: int) -> str:
    used_mb = max(0, current_bytes) / (1024 * 1024)
    return f"{used_mb:.1f} / {budget_mb} MB"
