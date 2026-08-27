"""Reusable settings page widgets adapted from Figma node 231:1738."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QFontMetrics, QIcon, QMouseEvent
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

from joyread.core.archive.limits import GIB
from joyread.infrastructure.i18n.locale_service import (
    LANGUAGE_DISPLAY_OPTIONS,
    language_display_name,
    language_value_from_display,
    t,
)
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import (
    ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MAX_SECONDS,
    ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MIN_SECONDS,
    ARCHIVE_GLOBAL_FILE_DEPTH_MAX,
    ARCHIVE_GLOBAL_FILE_DEPTH_MIN,
    ARCHIVE_MAX_EXTRACTED_ITEM_MAX_GB,
    ARCHIVE_MAX_EXTRACTED_ITEM_MIN_GB,
    ARCHIVE_MAX_IMAGE_MEGAPIXELS_MAX,
    ARCHIVE_MAX_IMAGE_MEGAPIXELS_MIN,
    ARCHIVE_MAX_OPERATION_DATA_MAX_GB,
    ARCHIVE_MAX_OPERATION_DATA_MIN_GB,
    ARCHIVE_MAX_SOURCE_SIZE_MAX_GB,
    ARCHIVE_MAX_SOURCE_SIZE_MIN_GB,
    ARCHIVE_POOL_MAX_GB,
    ARCHIVE_POOL_MIN_GB,
    ARCHIVE_CACHE_STRATEGY_OPTIONS,
    THUMBNAIL_CACHE_MAX_MB,
    THUMBNAIL_CACHE_MIN_MB,
    IMPORT_FOLDER_DEPTH_MAX,
    IMPORT_FOLDER_DEPTH_MIN,
    NESTED_ARCHIVE_DEPTH_MAX,
    NESTED_ARCHIVE_DEPTH_MIN,
    READER_PAGE_CACHE_MAX_MB,
    READER_PAGE_CACHE_MIN_MB,
    SettingsSectionKey,
    SettingsViewModel,
    UNLIMITED_DEPTH,
)
from joyread.ui.viewmodels.tag_management_viewmodel import TagManagementViewModel
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.menus import FigmaMenu
from joyread.ui.widgets.section_banner import SectionBanner


class SettingsPageWidget(QFrame):
    # Storage management (Privacy > Storage). Move picks a parent folder for a
    # fresh ``<parent>/JoyRead-Library``; Select adopts an existing JoyRead
    # library; Reset wipes the current library. MainWindow owns the
    # dialogs/file pickers.
    storage_move_requested = QtSignal()
    storage_select_requested = QtSignal()
    storage_reset_requested = QtSignal()
    # Hidden Space user-actions that need a dialog overlay. SettingsView
    # forwards them so MainWindow (which owns the overlay) can drive the
    # dialog flow and call the relevant VM methods.
    hidden_space_setup_requested = QtSignal()
    hidden_space_verify_requested = QtSignal()
    hidden_space_change_password_requested = QtSignal()
    hidden_space_revert_requested = QtSignal()
    hidden_space_reset_requested = QtSignal()
    # Tag CRUD outcomes. MainWindow routes these into ``JoyReadDialogOverlay.show_info``.
    tag_operation_completed = QtSignal(bool, str, str)
    tag_delete_requested = QtSignal(str, str)

    def __init__(
        self,
        viewmodel: SettingsViewModel,
        resources: ResourceLoader,
        parent: QWidget | None = None,
        *,
        tag_viewmodel: TagManagementViewModel | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._resources = resources
        self._tag_viewmodel = tag_viewmodel
        self._tag_page = None  # cached TagManagementPage, lazily created
        self._archive_pool_usage_item: SettingsCacheStatusItem | None = None
        self._disposed = False
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
        self._viewmodel.archive_pool_usage_changed.connect(self._refresh_archive_pool_usage)
        self.destroyed.connect(self._handle_destroyed)
        self.render()

    def sizeHint(self) -> QSize:
        return QSize(Theme.settings_panel_width, Theme.settings_panel_height)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._viewmodel.state_changed.disconnect(self.render)
        self._viewmodel.archive_pool_usage_changed.disconnect(self._refresh_archive_pool_usage)
        if self._tag_page is not None:
            self._tag_page.dispose()
            self._tag_page = None

    def render(self) -> None:
        if self._disposed:
            return
        self._sidebar.set_active(self._viewmodel.current_section)
        # Leaving the Tags section clears any chip selection so revisiting
        # the page starts in a clean state (and the inline rename input,
        # if it was open, is dropped).
        if (
            self._tag_viewmodel is not None
            and self._viewmodel.current_section != SettingsSectionKey.TAGS
        ):
            self._tag_viewmodel.clear_selection()
        self._content.set_items(self._items_for_current_section())
        # Only the cache section carries a usage row; elsewhere this is None
        # and the usage signal has nothing to update.
        self._archive_pool_usage_item = self._content.findChild(SettingsCacheStatusItem)

    def _refresh_archive_pool_usage(self) -> None:
        """Update the pool-usage label in place.

        The figure changes on its own while books are being cached. Re-running
        ``render()`` for it would destroy and rebuild every control in the
        section about once a second -- and a dropdown the user has open is one
        of those controls.
        """

        if self._disposed or self._archive_pool_usage_item is None:
            return
        self._archive_pool_usage_item.set_usage(
            self._viewmodel.archive_pool_current_bytes,
            self._viewmodel.archive_extraction_pool_gb * GIB,
        )

    def refresh_labels(self) -> None:
        """Refresh static labels and rebuild current content after a language change."""
        self._sidebar.refresh_labels()
        if self._tag_page is not None:
            self._tag_page.refresh_labels()
        self.render()

    def _handle_destroyed(self, _obj: object | None = None) -> None:
        self.dispose()

    def _items_for_current_section(self) -> list[QWidget]:
        if self._viewmodel.current_section == SettingsSectionKey.GENERAL:
            return self._general_items()
        if self._viewmodel.current_section == SettingsSectionKey.ARCHIVE:
            return self._archive_cache_items()
        if self._viewmodel.current_section == SettingsSectionKey.PRIVACY:
            return self._privacy_items()
        if self._viewmodel.current_section == SettingsSectionKey.TAGS:
            return self._tags_items()
        return []

    def _tags_items(self) -> list[QWidget]:
        # Lazy import to avoid a circular dependency between the settings
        # page and the tag management page (which imports SettingsPushButton
        # from this module).
        from joyread.ui.widgets.tag_management_page import TagManagementPage

        banner = SectionBanner(t("settings.banner_book_tag"), self._resources)
        if self._tag_viewmodel is None:
            return [banner]
        # Cache the page across section navigations. SettingsContentPanel
        # honours the ``persistent="true"`` property and skips deleteLater,
        # so the same page (and its viewmodel subscription) is reused.
        if self._tag_page is None:
            page = TagManagementPage(self._tag_viewmodel)
            page.setProperty("persistent", "true")
            page.tag_operation_completed.connect(self.tag_operation_completed.emit)
            page.tag_delete_requested.connect(self.tag_delete_requested.emit)
            self._tag_page = page
        self._tag_viewmodel.refresh()
        return [banner, self._tag_page]

    def _privacy_items(self) -> list[QWidget]:
        # Hidden Space surface. The switch row drives the dialog flow via
        # ``hidden_space_setup_requested`` / ``hidden_space_verify_requested``
        # — the VM only flips the persisted toggle off; turning it on
        # requires a password and is owned by MainWindow.
        hidden_banner = SectionBanner(t("settings.banner_hidden_space"), self._resources)

        show_switch = SettingsSwitchItem(
            t("settings.show_collections"),
            self._viewmodel.show_hidden_collection,
        )
        show_switch.toggled.connect(self._handle_show_hidden_toggled)

        change_password = SettingsButtonItem(t("settings.change_password"), t("settings.btn_change"))
        change_password.clicked.connect(self.hidden_space_change_password_requested.emit)
        # Hidden Space hasn't been set up yet → no password to change.
        change_password.set_enabled(self._viewmodel.hidden_space_initialized)

        revert = SettingsButtonItem(t("settings.revert_all"), t("settings.btn_proceed"))
        revert.clicked.connect(self.hidden_space_revert_requested.emit)
        revert.set_enabled(self._viewmodel.hidden_space_initialized)

        reset = SettingsButtonItem(t("settings.reset_and_erase"), t("settings.btn_proceed"), destructive=True)
        reset.clicked.connect(self.hidden_space_reset_requested.emit)
        reset.set_enabled(self._viewmodel.hidden_space_initialized)

        # Storage management. The library location itself is not shown/edited
        # here: Move creates a fresh JoyRead-Library under a chosen parent and
        # migrates, Select adopts an existing JoyRead library root, Reset wipes
        # the current one.
        storage_banner = SectionBanner(t("settings.banner_storage"), self._resources)

        # Show the current library directory (read-only) with a Move action,
        # matching the previous Storage Location row.
        move_library = SettingsAddressItem(
            t("settings.library_location"),
            self._viewmodel.storage_location,
            button_text=t("settings.btn_move"),
        )
        move_library.change_requested.connect(self.storage_move_requested.emit)

        select_library = SettingsButtonItem(t("settings.select_existing_library"), t("settings.btn_select"))
        select_library.clicked.connect(self.storage_select_requested.emit)

        reset_library = SettingsButtonItem(t("settings.reset_library"), t("settings.btn_proceed"), destructive=True)
        reset_library.clicked.connect(self.storage_reset_requested.emit)

        # Encrypted-archive cache. Extracted pages of a password-protected
        # archive are plaintext in the pool, which is not itself encrypted yet.
        encrypted_banner = SectionBanner(t("settings.banner_encrypted_cache"), self._resources)
        purge_encrypted = SettingsSwitchItem(
            t("settings.purge_encrypted_cache"),
            self._viewmodel.purge_encrypted_cache_on_close,
        )
        purge_encrypted.toggled.connect(self._viewmodel.set_purge_encrypted_cache_on_close)

        return [
            hidden_banner,
            show_switch,
            change_password,
            revert,
            reset,
            encrypted_banner,
            purge_encrypted,
            storage_banner,
            move_library,
            select_library,
            reset_library,
        ]

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
        general_banner = SectionBanner(t("settings.banner_general"), self._resources)

        language = SettingsDropdownItem(
            t("settings.language"),
            language_display_name(self._viewmodel.language),
            LANGUAGE_DISPLAY_OPTIONS,
            self._resources,
        )
        language.value_changed.connect(lambda label: self._viewmodel.set_language(language_value_from_display(label)))

        import_switch = SettingsSwitchItem(
            t("settings.import_when_opening"),
            self._viewmodel.import_book_when_opening,
        )
        import_switch.toggled.connect(self._viewmodel.set_import_book_when_opening)

        verify_import_switch = SettingsSwitchItem(
            t("settings.verify_imported_file_integrity"),
            self._viewmodel.verify_imported_file_integrity,
        )
        verify_import_switch.toggled.connect(self._viewmodel.set_verify_imported_file_integrity)

        window_switch = SettingsSwitchItem(
            t("settings.individual_read_window"),
            self._viewmodel.individual_read_window,
        )
        window_switch.toggled.connect(self._viewmodel.set_individual_read_window)

        inspect_title_switch = SettingsSwitchItem(
            t("settings.inspect_title_control"),
            self._viewmodel.inspect_non_native_title_control,
        )
        inspect_title_switch.toggled.connect(self._viewmodel.set_inspect_non_native_title_control)

        import_banner = SectionBanner(t("settings.banner_import"), self._resources)

        import_folder_depth_item = SettingsNumericItem(
            t("settings.import_folder_depth"),
            self._viewmodel.import_folder_max_depth,
            IMPORT_FOLDER_DEPTH_MIN,
            IMPORT_FOLDER_DEPTH_MAX,
            self._resources,
        )
        import_folder_depth_item.value_changed.connect(self._viewmodel.set_import_folder_max_depth)

        canonical_policy_item = SettingsDropdownItem(
            t("settings.canonical_import_policy"),
            self._viewmodel.canonical_import_policy_label,
            self._viewmodel.canonical_import_policy_options,
            self._resources,
        )
        canonical_policy_item.value_changed.connect(
            self._viewmodel.set_canonical_import_policy
        )

        library_banner = SectionBanner(t("settings.banner_library"), self._resources)
        verify_library = SettingsButtonItem(
            t("settings.verify_library_and_clean_cache"),
            t("settings.btn_verify"),
        )
        verify_library.clicked.connect(self._viewmodel.request_library_maintenance)


        return [
            general_banner,
            language,
            import_switch,
            verify_import_switch,
            window_switch,
            inspect_title_switch,
            import_banner,
            import_folder_depth_item,
            canonical_policy_item,
            library_banner,
            verify_library,
        ]

    def _archive_cache_items(self) -> list[QWidget]:
        """Resource budgets: how far into an archive to go, and how much to keep.

        Split out of ``_general_items`` because General had accumulated five
        banner groups against one each for Tags and Privacy. The two archive
        depth limits moved here from the Import group: the reader reads them
        too (``reader_shell``, ``reader_viewmodel``), so they were never
        import-specific despite the label they sat under.
        """

        archive_banner = SectionBanner(t("settings.banner_archive"), self._resources)
        nested_archive_depth_item = SettingsNumericItem(
            t("settings.nested_archive_depth"),
            self._viewmodel.nested_archive_max_depth,
            NESTED_ARCHIVE_DEPTH_MIN,
            NESTED_ARCHIVE_DEPTH_MAX,
            self._resources,
            unlimited_sentinel=UNLIMITED_DEPTH,
        )
        nested_archive_depth_item.value_changed.connect(self._viewmodel.set_nested_archive_max_depth)

        archive_global_depth_item = SettingsNumericItem(
            t("settings.archive_global_file_depth"),
            self._viewmodel.archive_global_file_max_depth,
            ARCHIVE_GLOBAL_FILE_DEPTH_MIN,
            ARCHIVE_GLOBAL_FILE_DEPTH_MAX,
            self._resources,
            unlimited_sentinel=UNLIMITED_DEPTH,
        )
        archive_global_depth_item.value_changed.connect(self._viewmodel.set_archive_global_file_max_depth)


        archive_size_switch = SettingsSwitchItem(
            t("settings.archive_max_source_size_enabled"),
            self._viewmodel.archive_max_source_size_enabled,
            gate=True,
        )
        archive_size_switch.toggled.connect(self._viewmodel.set_archive_max_source_size_enabled)

        archive_size_item = SettingsNumericItem(
            t("settings.archive_max_source_size"),
            self._viewmodel.archive_max_source_size_gb,
            ARCHIVE_MAX_SOURCE_SIZE_MIN_GB,
            ARCHIVE_MAX_SOURCE_SIZE_MAX_GB,
            self._resources,
            "GB",
        )
        archive_size_item.setEnabled(self._viewmodel.archive_max_source_size_enabled)
        archive_size_item.value_changed.connect(self._viewmodel.set_archive_max_source_size_gb)

        guardrails_switch = SettingsSwitchItem(
            t("settings.archive_resource_guardrails"),
            self._viewmodel.archive_resource_guardrails_enabled,
            gate=True,
        )
        guardrails_switch.toggled.connect(self._viewmodel.set_archive_resource_guardrails_enabled)

        extracted_item = SettingsNumericItem(
            t("settings.archive_max_extracted_item"),
            self._viewmodel.archive_max_extracted_item_gb,
            ARCHIVE_MAX_EXTRACTED_ITEM_MIN_GB,
            ARCHIVE_MAX_EXTRACTED_ITEM_MAX_GB,
            self._resources,
            "GB",
            unlimited_sentinel=UNLIMITED_DEPTH,
        )
        extracted_item.setEnabled(self._viewmodel.archive_resource_guardrails_enabled)
        extracted_item.value_changed.connect(self._viewmodel.set_archive_max_extracted_item_gb)

        operation_data = SettingsNumericItem(
            t("settings.archive_max_operation_data"),
            self._viewmodel.archive_max_operation_data_gb,
            ARCHIVE_MAX_OPERATION_DATA_MIN_GB,
            ARCHIVE_MAX_OPERATION_DATA_MAX_GB,
            self._resources,
            "GB",
            unlimited_sentinel=UNLIMITED_DEPTH,
        )
        operation_data.setEnabled(self._viewmodel.archive_resource_guardrails_enabled)
        operation_data.value_changed.connect(self._viewmodel.set_archive_max_operation_data_gb)

        image_megapixels = SettingsNumericItem(
            t("settings.archive_max_image_megapixels"),
            self._viewmodel.archive_max_image_megapixels,
            ARCHIVE_MAX_IMAGE_MEGAPIXELS_MIN,
            ARCHIVE_MAX_IMAGE_MEGAPIXELS_MAX,
            self._resources,
            "MP",
            unlimited_sentinel=UNLIMITED_DEPTH,
        )
        image_megapixels.setEnabled(self._viewmodel.archive_resource_guardrails_enabled)
        image_megapixels.value_changed.connect(self._viewmodel.set_archive_max_image_megapixels)

        command_timeout = SettingsNumericItem(
            t("settings.archive_external_command_timeout"),
            self._viewmodel.archive_external_command_timeout_seconds,
            ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MIN_SECONDS,
            ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MAX_SECONDS,
            self._resources,
            "s",
            unlimited_sentinel=UNLIMITED_DEPTH,
        )
        command_timeout.setEnabled(self._viewmodel.archive_resource_guardrails_enabled)
        command_timeout.value_changed.connect(self._viewmodel.set_archive_external_command_timeout_seconds)

        # Cache sub-group: user-tunable cache budgets and a one-shot purge for
        # the disk pool. Live in General per design — there is no separate
        # "Performance" section.
        cache_banner = SectionBanner(t("settings.banner_cache"), self._resources)

        reader_cache_item = SettingsNumericItem(
            t("settings.reader_page_cache"),
            self._viewmodel.reader_page_cache_mb,
            READER_PAGE_CACHE_MIN_MB,
            READER_PAGE_CACHE_MAX_MB,
            self._resources,
            "MB",
        )
        reader_cache_item.value_changed.connect(self._viewmodel.set_reader_page_cache_mb)

        detail_cache_item = SettingsNumericItem(
            t("settings.thumbnail_cache"),
            self._viewmodel.thumbnail_cache_mb,
            THUMBNAIL_CACHE_MIN_MB,
            THUMBNAIL_CACHE_MAX_MB,
            self._resources,
            "MB",
        )
        detail_cache_item.value_changed.connect(self._viewmodel.set_thumbnail_cache_mb)

        archive_pool_item = SettingsNumericItem(
            t("settings.archive_extraction_pool"),
            self._viewmodel.archive_extraction_pool_gb,
            ARCHIVE_POOL_MIN_GB,
            ARCHIVE_POOL_MAX_GB,
            self._resources,
            "GB",
        )
        archive_pool_item.value_changed.connect(self._viewmodel.set_archive_extraction_pool_gb)

        archive_strategy_item = SettingsDropdownItem(
            t("settings.archive_cache_strategy"),
            self._viewmodel.archive_cache_strategy_label,
            ARCHIVE_CACHE_STRATEGY_OPTIONS,
            self._resources,
        )
        archive_strategy_item.value_changed.connect(self._viewmodel.set_archive_cache_strategy)

        archive_pool_usage = SettingsCacheStatusItem(
            t("settings.archive_pool_usage"),
            current_bytes=self._viewmodel.archive_pool_current_bytes,
            budget_bytes=self._viewmodel.archive_extraction_pool_gb * GIB,
        )
        archive_pool_usage.clear_requested.connect(self._viewmodel.request_clear_archive_pool)


        return [
            archive_banner,
            nested_archive_depth_item,
            archive_global_depth_item,
            archive_size_switch,
            archive_size_item,
            guardrails_switch,
            extracted_item,
            operation_data,
            image_megapixels,
            command_timeout,
            cache_banner,
            reader_cache_item,
            detail_cache_item,
            archive_pool_item,
            archive_strategy_item,
            archive_pool_usage,
        ]



class SettingsSidebarWidget(QFrame):
    # Maps section key to the locale key used for the label.
    _SECTION_LOCALE_KEYS: dict[SettingsSectionKey, str] = {
        SettingsSectionKey.GENERAL: "settings.section_general",
        SettingsSectionKey.ARCHIVE: "settings.section_archive",
        SettingsSectionKey.TAGS: "settings.section_tags",
        SettingsSectionKey.PRIVACY: "settings.section_privacy",
        SettingsSectionKey.ABOUT: "settings.section_about",
    }

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

    def refresh_labels(self) -> None:
        """Re-apply translated labels to all sidebar items (called on language change)."""
        for section_key, item in self._items.items():
            locale_key = self._SECTION_LOCALE_KEYS.get(section_key)
            if locale_key:
                item.set_label(t(locale_key))


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

        self._text = QLabel(label)
        self._text.setProperty("class", "SettingsSidebarItemText")
        self._text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._text)
        layout.addStretch(1)

    def set_label(self, label: str) -> None:
        self._text.setText(label)

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
                # Persistent widgets (currently: TagManagementPage) keep
                # a viewmodel subscription and re-render on demand, so we
                # cannot deleteLater them on every section nav — their
                # cached subscription would fire against a destroyed
                # FlowLayout on the next visit.
                if widget.property("persistent") != "true":
                    widget.deleteLater()
        for item in items:
            self._layout.addWidget(item)
        self._layout.addStretch(1)


class SettingsOptionItem(QFrame):
    def __init__(
        self,
        name: str,
        option: QWidget,
        parent: QWidget | None = None,
        *,
        gate: bool = False,
    ) -> None:
        """``gate=True`` marks a row that switches the rows beneath it on or off.

        QSS bolds the label so the hierarchy is visible: the guardrail toggles
        read as headings for the limits they control rather than as one more
        setting in the list.
        """

        super().__init__(parent)
        self.setProperty("class", "SettingsItem")
        if gate:
            self.setProperty("gate", "true")
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

    def __init__(
        self,
        name: str,
        checked: bool,
        parent: QWidget | None = None,
        *,
        gate: bool = False,
    ) -> None:
        self.switch = SettingsSwitchControl(checked)
        # Pass the switch directly (no wrapper) so its right edge lines up
        # with the buttons / dropdowns / spinners in the column — the Figma
        # right-aligns every option control to the same gutter.
        super().__init__(name, self.switch, parent, gate=gate)
        self.switch.toggled.connect(self.toggled.emit)


class SettingsButtonItem(SettingsOptionItem):
    """Label + right-aligned push button row (Figma node I231:1711;509:3236).

    Used for the Hidden Space rows (Change Password, Revert all, Reset and
    Erase). When ``destructive=True`` the ``destructive`` property is set
    on both the row frame and the push button so the QSS rule under
    ``QFrame[class="SettingsItem"][destructive="true"]`` tints the label
    and the caption red — no inline stylesheets.
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
            self.button.setProperty("destructive", "true")
        super().__init__(name, self.button, parent)
        if destructive:
            self.setProperty("destructive", "true")
            # Property changes after construction need a polish pass for
            # QSS attribute selectors to recompute.
            self.style().unpolish(self)
            self.style().polish(self)
            self.button.style().unpolish(self.button)
            self.button.style().polish(self.button)
        self.button.clicked.connect(self.clicked.emit)

    def set_enabled(self, enabled: bool) -> None:
        self.button.setEnabled(enabled)


class SettingsAddressItem(QFrame):
    change_requested = QtSignal()

    def __init__(
        self,
        title: str,
        directory: str,
        parent: QWidget | None = None,
        *,
        button_text: str = "Change",
    ) -> None:
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

        change_button = SettingsPushButton(button_text)
        change_button.clicked.connect(self.change_requested.emit)
        option_layout.addWidget(change_button)

        layout.addWidget(option)


def _dropdown_width(options: tuple[str, ...]) -> int:
    """Wide enough for the longest option, never narrower than the design width.

    Measured across *every* option rather than the current one, so choosing a
    different value does not resize the control under the user's cursor. The
    Figma width stays the floor, so the short dropdowns that already fit are
    pixel-identical -- only one that would otherwise render its own default
    clipped ("...sive and nested fo...") grows.
    """

    metrics = QFontMetrics(QLabel().font())
    widest = max((metrics.horizontalAdvance(option) for option in options), default=0)
    needed = widest + Theme.settings_dropdown_indicator_width + _DROPDOWN_TEXT_PADDING
    return max(Theme.settings_dropdown_width, needed)


#: Breathing room either side of the label, so text never touches the border or
#: the chevron.
_DROPDOWN_TEXT_PADDING = 20


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
        self.setFixedSize(_dropdown_width(options), Theme.settings_dropdown_height)

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
        self._sync_checked_property()
        self._position_knob()

    @property
    def checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._sync_checked_property()
        self._position_knob()
        self.toggled.emit(checked)

    def _sync_checked_property(self) -> None:
        # Drives the QSS attribute selector that paints the "on" state
        # with the Figma white track instead of the gray-track default.
        self.setProperty("checked", "true" if self._checked else "false")
        self.style().unpolish(self)
        self.style().polish(self)

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
        *,
        unlimited_sentinel: int | None = None,
    ) -> None:
        self.spin_button = SettingsSpinButtonSmall(
            value,
            minimum,
            maximum,
            suffix,
            resources,
            unlimited_sentinel=unlimited_sentinel,
        )
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
        *,
        unlimited_sentinel: int | None = None,
    ) -> None:
        super().__init__(parent)
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._unlimited_sentinel = unlimited_sentinel
        self._unit = unit
        self._value = self._normalized_value(value, fallback=self._minimum)
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
        value_lengths = [len(str(self._minimum)), len(str(self._maximum))]
        if self._unlimited_sentinel is not None:
            value_lengths.append(len(str(self._unlimited_sentinel)))
        self._value_editor.setMaxLength(max(value_lengths))
        self._value_editor.setFixedWidth(Theme.settings_spin_editor_width)
        if self._unlimited_sentinel is None:
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
        normalized = self._normalized_value(value, fallback=self._value)
        if normalized == self._value:
            self._refresh_label()
            return
        self._value = normalized
        self._refresh_label()
        if emit:
            self.value_changed.emit(normalized)

    def step_by(self, delta: int) -> None:
        if self._unlimited_sentinel is not None:
            if self._value == self._unlimited_sentinel and delta > 0:
                self.set_value(self._minimum)
                return
            if self._value == self._minimum and delta < 0:
                self.set_value(self._unlimited_sentinel)
                return
        self.set_value(self._value + delta)

    def _normalized_value(self, value: object, *, fallback: int) -> int:
        try:
            coerced = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback
        if self._unlimited_sentinel is not None and coerced == self._unlimited_sentinel:
            return self._unlimited_sentinel
        if self._unlimited_sentinel is not None and coerced < self._minimum:
            return fallback
        return max(self._minimum, min(self._maximum, coerced))

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
        budget_bytes: int,
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

        self._usage_label = QLabel(_format_usage_label(current_bytes, budget_bytes))
        self._usage_label.setObjectName("SettingsCacheUsageLabel")
        self._usage_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        option_layout.addWidget(self._usage_label, stretch=1)

        clear_button = SettingsPushButton(t("settings.btn_clear"))
        clear_button.clicked.connect(self.clear_requested.emit)
        option_layout.addWidget(clear_button)

        layout.addWidget(option)

    def set_usage(self, current_bytes: int, budget_bytes: int) -> None:
        self._usage_label.setText(_format_usage_label(current_bytes, budget_bytes))


def _format_usage_label(current_bytes: int, budget_bytes: int) -> str:
    used_gb = max(0, current_bytes) / GIB
    budget_gb = max(0, budget_bytes) / GIB
    return f"{used_gb:.1f} / {budget_gb:g} GB"
