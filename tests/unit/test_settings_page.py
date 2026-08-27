import json

import shiboken6
from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QToolButton, QWidget

from joyread.app.app_context import create_app_context
from joyread.core.archive.limits import GIB, MEGAPIXEL
from joyread.core.models.cache import ArchiveCacheStrategy
from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsSectionKey, SettingsViewModel
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.settings_view import SettingsView
from joyread.ui.widgets.menus import FigmaMenu
from tests.support.qt_events import MenuLoopWatchdog, flush_deferred_deletes
from joyread.ui.widgets.settings_page import (
    SettingsAddressItem,
    SettingsCacheStatusItem,
    SettingsContentPanel,
    SettingsDropdownButton,
    SettingsOptionItem,
    SettingsPageWidget,
    SettingsSidebarItem,
    SettingsSpinButtonSmall,
    SettingsSwitchControl,
    SettingsSwitchItem,
)


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def test_settings_viewmodel_tracks_section_and_general_options() -> None:
    viewmodel = SettingsViewModel()
    changes: list[None] = []
    viewmodel.state_changed.connect(lambda: changes.append(None))

    viewmodel.set_section(SettingsSectionKey.TAGS)
    viewmodel.set_import_book_when_opening(True)
    viewmodel.set_individual_read_window(True)
    viewmodel.set_inspect_non_native_title_control(True)
    viewmodel.set_language("English")
    viewmodel.set_storage_location("~/Documents/JoyRead-Library-Test")
    viewmodel.set_archive_cache_strategy("Hidden image files")
    viewmodel.set_import_folder_max_depth(3)
    viewmodel.set_nested_archive_max_depth(4)
    viewmodel.set_archive_global_file_max_depth(250)
    viewmodel.set_archive_max_source_size_enabled(False)
    viewmodel.set_archive_max_source_size_gb(12)
    viewmodel.set_archive_resource_guardrails_enabled(False)
    viewmodel.set_archive_max_extracted_item_gb(8)
    viewmodel.set_archive_max_operation_data_gb(32)
    viewmodel.set_archive_max_image_megapixels(800)
    viewmodel.set_archive_external_command_timeout_seconds(900)

    assert viewmodel.current_section == SettingsSectionKey.TAGS
    assert viewmodel.import_book_when_opening is True
    assert viewmodel.individual_read_window is True
    assert viewmodel.inspect_non_native_title_control is True
    assert viewmodel.storage_location == "~/Documents/JoyRead-Library-Test"
    assert viewmodel.archive_cache_strategy == ArchiveCacheStrategy.HIDDEN_IMAGE_FILES
    assert viewmodel.archive_cache_strategy_label == "Hidden image files"
    assert viewmodel.import_folder_max_depth == 3
    assert viewmodel.nested_archive_max_depth == 4
    assert viewmodel.archive_global_file_max_depth == 250
    assert viewmodel.archive_max_source_size_enabled is False
    assert viewmodel.archive_max_source_size_gb == 12
    assert viewmodel.archive_resource_guardrails_enabled is False
    assert viewmodel.archive_max_extracted_item_gb == 8
    assert viewmodel.archive_max_operation_data_gb == 32
    assert viewmodel.archive_max_image_megapixels == 800
    assert viewmodel.archive_external_command_timeout_seconds == 900
    assert len(changes) == 16


def test_settings_viewmodel_accepts_unlimited_depth_and_ignores_invalid_sentinels() -> None:
    viewmodel = SettingsViewModel()
    depth_changes: list[None] = []
    viewmodel.archive_depth_limits_changed.connect(lambda: depth_changes.append(None))

    viewmodel.set_nested_archive_max_depth(-1)
    viewmodel.set_archive_global_file_max_depth(-1)

    assert viewmodel.nested_archive_max_depth == -1
    assert viewmodel.archive_global_file_max_depth == -1
    assert viewmodel.archive_open_limits.nested_archive_max_depth is None
    assert viewmodel.archive_open_limits.global_file_max_depth is None

    viewmodel.set_nested_archive_max_depth(0)
    viewmodel.set_archive_global_file_max_depth(-2)

    assert viewmodel.nested_archive_max_depth == -1
    assert viewmodel.archive_global_file_max_depth == -1
    assert len(depth_changes) == 2


def test_settings_viewmodel_converts_archive_guardrails_to_none_without_losing_values() -> None:
    viewmodel = SettingsViewModel()
    limit_changes: list[None] = []
    viewmodel.archive_open_limits_changed.connect(lambda: limit_changes.append(None))

    viewmodel.set_archive_max_source_size_gb(9)
    viewmodel.set_archive_max_extracted_item_gb(-1)
    viewmodel.set_archive_max_operation_data_gb(12)
    viewmodel.set_archive_max_image_megapixels(250)
    viewmodel.set_archive_external_command_timeout_seconds(45)

    limits = viewmodel.archive_open_limits
    assert limits.max_source_bytes == 9 * GIB
    assert limits.max_extracted_item_bytes is None
    assert limits.max_operation_bytes == 12 * GIB
    assert limits.max_image_pixels == 250 * MEGAPIXEL
    assert limits.external_command_timeout_seconds == 45

    viewmodel.set_archive_resource_guardrails_enabled(False)
    disabled = viewmodel.archive_open_limits
    assert disabled.max_source_bytes == 9 * GIB
    assert disabled.max_extracted_item_bytes is None
    assert disabled.max_operation_bytes is None
    assert disabled.max_image_pixels is None
    assert disabled.external_command_timeout_seconds is None
    assert viewmodel.archive_max_operation_data_gb == 12

    viewmodel.set_archive_resource_guardrails_enabled(True)
    restored = viewmodel.archive_open_limits
    assert restored.max_operation_bytes == 12 * GIB
    assert restored.max_image_pixels == 250 * MEGAPIXEL
    assert restored.external_command_timeout_seconds == 45

    viewmodel.set_archive_max_operation_data_gb(0)
    viewmodel.set_archive_max_image_megapixels(-2)
    assert viewmodel.archive_max_operation_data_gb == 12
    assert viewmodel.archive_max_image_megapixels == 250
    assert len(limit_changes) == 7


def test_settings_page_matches_figma_panel_sidebar_and_content_geometry(qtbot) -> None:
    apply_theme()
    page = SettingsPageWidget(SettingsViewModel(), ResourceLoader())
    qtbot.addWidget(page)
    page.resize(Theme.settings_panel_width, Theme.settings_panel_height)
    page.show()
    QApplication.processEvents()

    margins = page.layout().contentsMargins()
    sidebar = page.findChild(QFrame, "SettingsSidebar")
    content = page.findChild(SettingsContentPanel)
    sidebar_items = page.findChildren(SettingsSidebarItem)
    setting_items = [item for item in page.findChildren(QFrame) if item.property("class") == "SettingsItem"]

    assert page.sizeHint().width() == Theme.settings_panel_width
    assert page.sizeHint().height() == Theme.settings_panel_height
    assert page.minimumWidth() == Theme.settings_panel_min_width
    assert page.maximumWidth() == Theme.settings_panel_max_width
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
        Theme.settings_panel_layout_margin,
        Theme.settings_panel_layout_margin,
        Theme.settings_panel_layout_margin,
        Theme.settings_panel_layout_margin,
    )
    assert page.layout().spacing() == Theme.settings_panel_gap
    assert sidebar is not None
    assert sidebar.width() == Theme.settings_sidebar_width
    sidebar_margins = sidebar.layout().contentsMargins()
    assert (sidebar_margins.left(), sidebar_margins.top(), sidebar_margins.right(), sidebar_margins.bottom()) == (
        Theme.settings_sidebar_layout_margin,
        Theme.settings_sidebar_layout_margin,
        Theme.settings_sidebar_layout_margin,
        Theme.settings_sidebar_layout_margin,
    )
    assert content is not None
    content_margins = content.widget().layout().contentsMargins()
    assert (content_margins.left(), content_margins.top(), content_margins.right(), content_margins.bottom()) == (
        Theme.settings_content_padding,
        Theme.settings_content_padding,
        Theme.settings_content_padding,
        Theme.settings_content_padding,
    )
    assert content.widget().layout().spacing() == Theme.settings_content_gap
    assert {item.height() for item in sidebar_items} == {Theme.settings_sidebar_item_height}
    sidebar_item_positions = {
        item.findChild(QLabel).text(): item.mapTo(sidebar, QPoint(0, 0)).y() for item in sidebar_items
    }
    step = Theme.settings_sidebar_item_height + Theme.settings_sidebar_gap
    # Walk the whole upper group rather than naming pairs, so inserting a
    # section cannot leave a stale assertion passing on the wrong neighbours.
    upper_order = ["General", "Archive & Cache", "Tags", "Privacy"]
    for earlier, later in zip(upper_order, upper_order[1:]):
        assert sidebar_item_positions[later] - sidebar_item_positions[earlier] == step
    assert sidebar_item_positions["About"] > Theme.settings_panel_height - 80
    # Five General rows (Storage moved to Privacy), the two genuinely
    # import-only rows (folder depth and the conversion policy), and the Library
    # maintenance action. Archive, Cache, and the two shared archive depth rows
    # are in their own scope now.
    assert len(setting_items) == 8
    spin_buttons = page.findChildren(SettingsSpinButtonSmall)
    assert len(spin_buttons) == 1
    assert {spin.size().width() for spin in spin_buttons} == {Theme.settings_spin_width}
    assert {spin.size().height() for spin in spin_buttons} == {Theme.settings_spin_height}


def test_general_tab_renders_inspection_title_control_switch(qtbot) -> None:
    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    QApplication.processEvents()

    labels = [
        label.text()
        for label in page.findChildren(QLabel)
        if label.property("class") == "SettingsItemNameText"
    ]
    switches = page.findChildren(SettingsSwitchItem)

    assert "Use Native Title Control" not in labels
    assert "Inspect Windows/Linux Title Control" in labels
    # Four: import-on-open, verify-integrity, individual window, inspect title.
    # The archive size and guardrail switches live in the Archive & Cache scope.
    assert len(switches) == 4

    inspect_item = next(
        item
        for item in page.findChildren(SettingsSwitchItem)
        if next(
            child
            for child in item.findChildren(QLabel)
            if child.property("class") == "SettingsItemNameText"
        ).text()
        == "Inspect Windows/Linux Title Control"
    )
    assert inspect_item.switch.isEnabled()

    inspect_item.switch.set_checked(True)
    QApplication.processEvents()

    assert viewmodel.inspect_non_native_title_control is True


def test_general_tab_library_maintenance_button_emits_viewmodel_request(qtbot) -> None:
    apply_theme()
    from joyread.ui.widgets.settings_page import SettingsButtonItem

    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    requested: list[bool] = []
    viewmodel.library_maintenance_requested.connect(lambda: requested.append(True))

    button = next(
        item
        for item in page.findChildren(SettingsButtonItem)
        if any(
            label.text() == "Verify Library & Clean Cache"
            for label in item.findChildren(QLabel)
        )
    )
    button.button.click()

    assert requested == [True]


def test_language_dropdown_displays_native_names_but_persists_canonical_value(qtbot, tmp_path) -> None:
    apply_theme()
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    settings = store.update(language="English")
    viewmodel = SettingsViewModel(settings, store)
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    QApplication.processEvents()

    language_dropdown = page.findChildren(SettingsDropdownButton)[0]

    assert language_dropdown.value == "English"
    assert language_dropdown._options == ("English", "中文", "日本語")

    language_dropdown.set_value("中文")
    QApplication.processEvents()

    assert viewmodel.language == "Chinese"
    assert store.load().language == "Chinese"
    locale_service.load_language("English")


def test_language_dropdown_selected_display_follows_canonical_viewmodel_value(qtbot) -> None:
    apply_theme()
    viewmodel = SettingsViewModel(AppSettings(storage_location="~/Library", language="Japanese"))
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    QApplication.processEvents()

    language_dropdown = page.findChildren(SettingsDropdownButton)[0]

    assert language_dropdown.value == "日本語"


def test_settings_page_refresh_labels_updates_sidebar_and_current_content(qtbot) -> None:
    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    QApplication.processEvents()

    assert any(item.findChild(QLabel).text() == "General" for item in page.findChildren(SettingsSidebarItem))

    locale_service.load_language("Chinese")
    page.refresh_labels()
    QApplication.processEvents()

    sidebar_labels = [item.findChild(QLabel).text() for item in page.findChildren(SettingsSidebarItem)]
    setting_labels = [
        label.text()
        for label in page.findChildren(QLabel)
        if label.property("class") == "SettingsItemNameText"
    ]

    assert "通用" in sidebar_labels
    assert "语言" in setting_labels
    locale_service.load_language("English")


def test_settings_content_panel_accepts_reusable_setting_item_classes(qtbot) -> None:
    apply_theme()
    panel = SettingsContentPanel()
    qtbot.addWidget(panel)

    panel.set_items(
        [
            SettingsSwitchItem("Example Switch", False),
            SettingsAddressItem("Example Path", "~/Example"),
        ]
    )

    assert len(panel.findChildren(SettingsSwitchItem)) == 1
    assert len(panel.findChildren(SettingsAddressItem)) == 1
    assert panel.widget().layout().count() == 3  # two setting items plus final stretch


def test_open_dropdown_does_not_wedge_when_a_re_render_destroys_it(qtbot) -> None:
    """A section re-render can land while one of its dropdowns is open.

    Archive-pool usage is reported from worker threads as a queued signal, and
    the settings page re-renders the current section on it -- so the rebuild
    arrives as a posted event, which the open menu's own loop delivers. That
    destroys the control the menu belongs to, and Qt sends no hide event for a
    destroyed widget: the loop has to end on its own or the window freezes.
    """

    apply_theme()
    panel = SettingsContentPanel()
    qtbot.addWidget(panel)
    control = SettingsDropdownButton("Slide", ("Slide", "Fade"), ResourceLoader())
    panel.set_items([SettingsOptionItem("Transition", control)])
    panel.show()
    qtbot.wait(0)
    watchdog = MenuLoopWatchdog()

    def re_render_the_section() -> None:
        watchdog.watch(panel.findChildren(FigmaMenu)[0]._loop)
        replacement = SettingsDropdownButton("Slide", ("Slide", "Fade"), ResourceLoader())
        panel.set_items([SettingsOptionItem("Transition", replacement)])
        flush_deferred_deletes()

    QTimer.singleShot(0, re_render_the_section)
    with watchdog:
        qtbot.mousePress(control, Qt.MouseButton.LeftButton)
        qtbot.mouseRelease(control, Qt.MouseButton.LeftButton)

    assert not shiboken6.isValid(control), "the re-render should have replaced the open dropdown"
    assert not watchdog.fired, "the dropdown kept waiting after the re-render destroyed it"


def test_pool_usage_updates_its_label_without_rebuilding_the_section(qtbot) -> None:
    """The usage figure moves on its own, and it feeds exactly one label.

    Rendering the whole section for it tears down and rebuilds every control
    in that section -- measured at eight rebuilds while caching a single 48 MB
    book -- and takes any popup the user has open along with them.
    """

    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.ARCHIVE)
    QApplication.processEvents()
    pool_bytes = [0]
    viewmodel.set_archive_pool_bytes_provider(lambda: pool_bytes[0])

    usage_item = page.findChild(SettingsCacheStatusItem)
    usage_label = page.findChild(QLabel, "SettingsCacheUsageLabel")
    assert usage_label.text() == "0.0 / 5 GB"

    pool_bytes[0] = 3 * GIB
    viewmodel.refresh_archive_pool_usage()

    assert page.findChild(SettingsCacheStatusItem) is usage_item, "the section was rebuilt for one label"
    assert usage_label.text() == "3.0 / 5 GB"


def test_pool_usage_after_leaving_the_cache_section_touches_nothing(qtbot) -> None:
    """Navigating away deletes the usage row, and the ticks keep coming.

    They arrive from the caching workers whatever section is on screen, so the
    page must not still be holding the row that navigation destroyed.
    """

    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.ARCHIVE)
    QApplication.processEvents()
    pool_bytes = [0]
    viewmodel.set_archive_pool_bytes_provider(lambda: pool_bytes[0])

    viewmodel.set_section(SettingsSectionKey.GENERAL)
    flush_deferred_deletes()

    pool_bytes[0] = 3 * GIB
    viewmodel.refresh_archive_pool_usage()

    assert page.findChild(SettingsCacheStatusItem) is None


def test_pool_usage_does_not_close_an_open_dropdown(qtbot) -> None:
    """Cache strategy sits in the same section as the usage figure.

    So the dropdown most exposed to a usage-driven rebuild is the one right
    next to it -- and while a book is caching those arrive about once a
    second, which is shorter than it takes to read two options and pick one.
    """

    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.ARCHIVE)
    page.show()
    qtbot.wait(0)
    pool_bytes = [0]
    viewmodel.set_archive_pool_bytes_provider(lambda: pool_bytes[0])
    dropdown = page.findChildren(SettingsDropdownButton)[0]
    picked: list[str] = []
    dropdown.value_changed.connect(picked.append)
    survived: list[bool] = []
    label_seen: list[str] = []
    watchdog = MenuLoopWatchdog(timeout_ms=500)

    def usage_tick_lands() -> None:
        menus = page.findChildren(FigmaMenu)
        watchdog.watch(menus[0]._loop)
        pool_bytes[0] = 3 * GIB
        viewmodel.refresh_archive_pool_usage()
        flush_deferred_deletes()

        survived.append(bool(page.findChildren(FigmaMenu)) and shiboken6.isValid(dropdown))
        label_seen.append(page.findChild(QLabel, "SettingsCacheUsageLabel").text())
        if not survived[-1]:
            return
        rows = [row for row in menus[0].findChildren(QFrame) if row.objectName() == "FigmaMenuItem"]
        qtbot.mousePress(rows[1], Qt.MouseButton.LeftButton)
        qtbot.mouseRelease(rows[1], Qt.MouseButton.LeftButton)

    QTimer.singleShot(0, usage_tick_lands)
    with watchdog:
        qtbot.mousePress(dropdown, Qt.MouseButton.LeftButton)
        qtbot.mouseRelease(dropdown, Qt.MouseButton.LeftButton)
    # MenuItem defers ``clicked``, then _trigger defers the callback: two turns.
    QApplication.processEvents()
    QApplication.processEvents()

    assert label_seen == ["3.0 / 5 GB"], "the usage change never reached the label"
    assert survived == [True], "the usage change closed the open dropdown"
    assert picked == ["Hidden image files"]


def test_settings_overlay_resizes_panel_within_figma_min_max(qtbot) -> None:
    apply_theme()
    overlay = SettingsView(SettingsViewModel(), ResourceLoader())
    qtbot.addWidget(overlay)

    overlay.resize(900, 600)
    overlay.show()
    QApplication.processEvents()
    assert overlay.page.width() == Theme.settings_panel_min_width
    assert overlay.page.height() == Theme.settings_panel_min_height
    assert overlay.page.geometry().center() == overlay.rect().center()

    overlay.resize(Theme.window_width, Theme.window_height)
    QApplication.processEvents()
    assert overlay.page.width() == Theme.settings_panel_width
    assert overlay.page.height() == Theme.settings_panel_height
    assert overlay.page.geometry().center() == overlay.rect().center()

    overlay.resize(2000, 1200)
    QApplication.processEvents()
    assert overlay.page.width() == Theme.settings_panel_max_width
    assert overlay.page.height() == Theme.settings_panel_max_height
    assert overlay.page.geometry().center() == overlay.rect().center()


def test_settings_switch_and_button_items_share_right_gutter(qtbot) -> None:
    # Privacy + General show switches and buttons in the same column; the
    # Figma right-aligns every option control to a common gutter, so the
    # switch row must not add extra horizontal padding around the switch.
    apply_theme()
    from joyread.ui.widgets.settings_page import SettingsButtonItem

    panel = SettingsContentPanel()
    qtbot.addWidget(panel)
    switch_item = SettingsSwitchItem("Switch", False)
    button_item = SettingsButtonItem("Button", "Change")
    panel.set_items([switch_item, button_item])
    panel.resize(420, 200)
    panel.show()
    QApplication.processEvents()

    switch_right = switch_item.switch.mapTo(panel, switch_item.switch.rect().topRight()).x()
    button_right = button_item.button.mapTo(panel, button_item.button.rect().topRight()).x()

    assert switch_right == button_right


def test_settings_switch_control_toggles_and_keeps_figma_knob_size(qtbot) -> None:
    apply_theme()
    switch = SettingsSwitchControl(False)
    qtbot.addWidget(switch)
    emitted: list[bool] = []
    switch.toggled.connect(emitted.append)
    knob = switch.findChild(QFrame)

    assert knob is not None
    assert switch.size().width() == Theme.settings_switch_width
    assert switch.size().height() == Theme.settings_switch_height
    assert knob.size().width() == Theme.settings_switch_knob_size
    assert knob.x() == Theme.settings_switch_layout_margin
    # Off state: QSS attribute selector picks the gray track variant.
    assert switch.property("checked") == "false"

    qtbot.mouseClick(switch, Qt.MouseButton.LeftButton)

    assert switch.checked is True
    assert emitted == [True]
    assert knob.x() == switch.width() - Theme.settings_switch_layout_margin - Theme.settings_switch_knob_size
    # On state flips the dynamic property so the white-track QSS rule
    # applies — the Figma small switch fills its track when on.
    assert switch.property("checked") == "true"


def test_settings_spin_button_small_matches_figma_geometry_and_steps(qtbot) -> None:
    apply_theme()
    spin = SettingsSpinButtonSmall(42, 8, 64, "%", ResourceLoader())
    qtbot.addWidget(spin)
    emitted: list[int] = []
    spin.value_changed.connect(emitted.append)

    spin.show()
    QApplication.processEvents()

    buttons = spin.findChildren(QToolButton)
    labels = spin.findChildren(QLabel)
    editor = spin.findChild(QLineEdit, "SettingsSpinValueEditor")

    assert spin.size().width() == Theme.settings_spin_width
    assert spin.size().height() == Theme.settings_spin_height
    assert editor is not None
    assert editor.width() == Theme.settings_spin_editor_width
    assert editor.text() == "42"
    assert len(buttons) == 2
    assert [button.property("iconName") for button in buttons] == ["icon_left.svg", "icon_right.svg"]
    assert {button.size().width() for button in buttons} == {Theme.settings_spin_step_button_width}
    assert {button.size().height() for button in buttons} == {Theme.settings_spin_height}
    assert [label.text() for label in labels] == ["%"]

    qtbot.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    assert spin.value == 43
    qtbot.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    assert spin.value == 42

    spin.set_value(999)
    assert spin.value == 64
    spin.set_value(-1)
    assert spin.value == 8
    assert emitted == [43, 42, 64, 8]


def test_settings_spin_button_small_commits_editor_value_on_return(qtbot) -> None:
    apply_theme()
    spin = SettingsSpinButtonSmall(42, 8, 64, "%", ResourceLoader())
    qtbot.addWidget(spin)
    emitted: list[int] = []
    spin.value_changed.connect(emitted.append)

    spin.show()
    QApplication.processEvents()

    editor = spin.findChild(QLineEdit, "SettingsSpinValueEditor")
    assert editor is not None
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClicks(editor, "55")

    assert spin.value == 42
    assert emitted == []

    qtbot.keyClick(editor, Qt.Key.Key_Return)

    assert spin.value == 55
    assert editor.text() == "55"
    assert emitted == [55]

    editor.selectAll()
    qtbot.keyClicks(editor, "99")
    assert spin.value == 55
    qtbot.keyClick(editor, Qt.Key.Key_Return)

    assert spin.value == 64
    assert editor.text() == "64"
    assert emitted == [55, 64]

    editor.clear()
    qtbot.keyClick(editor, Qt.Key.Key_Return)

    assert spin.value == 64
    assert editor.text() == "64"
    assert emitted == [55, 64]


def test_settings_depth_spin_supports_unlimited_and_rejects_zero(qtbot) -> None:
    apply_theme()
    spin = SettingsSpinButtonSmall(
        1,
        1,
        5,
        "",
        ResourceLoader(),
        unlimited_sentinel=-1,
    )
    qtbot.addWidget(spin)
    emitted: list[int] = []
    spin.value_changed.connect(emitted.append)
    spin.show()
    QApplication.processEvents()

    buttons = spin.findChildren(QToolButton)
    editor = spin.findChild(QLineEdit, "SettingsSpinValueEditor")
    assert editor is not None

    qtbot.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    assert spin.value == -1
    qtbot.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    assert spin.value == 1

    editor.setText("0")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    assert spin.value == 1
    assert editor.text() == "1"

    editor.setText("-2")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    assert spin.value == 1
    assert editor.text() == "1"
    assert emitted == [-1, 1]


def test_settings_store_migrates_legacy_archive_depth_and_persists_new_keys(tmp_path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    store.config_dir.mkdir(parents=True)
    store.settings_path.write_text(
        json.dumps(
            {
                "storage_location": str(tmp_path / "storage"),
                "archive_internal_max_depth": -1,
                "archive_global_file_max_depth": 0,
            }
        ),
        encoding="utf-8",
    )

    settings = store.load()

    assert settings.nested_archive_max_depth == -1
    assert settings.archive_global_file_max_depth == 100

    store.save(settings)
    saved = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert saved["nested_archive_max_depth"] == -1
    assert saved["archive_global_file_max_depth"] == 100
    assert "archive_internal_max_depth" not in saved


def test_settings_store_defaults_and_persists_archive_guardrail_values(tmp_path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    store.config_dir.mkdir(parents=True)
    store.settings_path.write_text(
        json.dumps(
            {
                "storage_location": str(tmp_path / "storage"),
                "archive_max_source_size_enabled": False,
                "archive_max_source_size_gb": 14,
                "archive_resource_guardrails_enabled": True,
                "archive_max_extracted_item_gb": -1,
                "archive_max_operation_data_gb": 63,
                "archive_max_image_megapixels": 999,
                "archive_external_command_timeout_seconds": -1,
            }
        ),
        encoding="utf-8",
    )

    settings = store.load()

    assert settings.archive_max_source_size_enabled is False
    assert settings.archive_max_source_size_gb == 14
    assert settings.archive_resource_guardrails_enabled is True
    assert settings.archive_max_extracted_item_gb == -1
    assert settings.archive_max_operation_data_gb == 63
    assert settings.archive_max_image_megapixels == 999
    assert settings.archive_external_command_timeout_seconds == -1

    store.save(settings)
    saved = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert saved["archive_max_source_size_enabled"] is False
    assert saved["archive_max_source_size_gb"] == 14
    assert saved["archive_resource_guardrails_enabled"] is True
    assert saved["archive_max_extracted_item_gb"] == -1
    assert saved["archive_max_operation_data_gb"] == 63
    assert saved["archive_max_image_megapixels"] == 999
    assert saved["archive_external_command_timeout_seconds"] == -1

    legacy_store = SettingsStore(
        support_root=tmp_path / "legacy-support",
        default_storage_root=tmp_path / "legacy-storage",
    )
    legacy_store.config_dir.mkdir(parents=True)
    legacy_store.settings_path.write_text(
        json.dumps({"storage_location": str(tmp_path / "legacy-storage")}),
        encoding="utf-8",
    )
    defaults = legacy_store.load()
    assert defaults.archive_max_source_size_enabled is True
    assert defaults.archive_max_source_size_gb == 5
    assert defaults.archive_resource_guardrails_enabled is True
    assert defaults.archive_max_extracted_item_gb == 1
    assert defaults.archive_max_operation_data_gb == 4
    assert defaults.archive_max_image_megapixels == 400
    assert defaults.archive_external_command_timeout_seconds == 300


def test_settings_store_migrates_thumbnail_cache_key_and_viewmodel_persists_new_name(tmp_path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    store.config_dir.mkdir(parents=True)
    store.settings_path.write_text(
        json.dumps(
            {
                "storage_location": str(tmp_path / "storage"),
                "detail_thumbnail_cache_mb": 72,
            }
        ),
        encoding="utf-8",
    )

    settings = store.load()
    viewmodel = SettingsViewModel(settings, store)

    assert settings.thumbnail_cache_mb == 72
    assert viewmodel.thumbnail_cache_mb == 72

    viewmodel.set_thumbnail_cache_mb(96)
    saved = json.loads(store.settings_path.read_text(encoding="utf-8"))

    assert saved["thumbnail_cache_mb"] == 96
    assert "detail_thumbnail_cache_mb" not in saved


def test_settings_store_migrates_archive_pool_mb_to_gb_and_saves_only_gb(tmp_path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    store.config_dir.mkdir(parents=True)
    store.settings_path.write_text(
        json.dumps(
            {
                "storage_location": str(tmp_path / "storage"),
                "archive_extraction_pool_mb": 1537,
            }
        ),
        encoding="utf-8",
    )

    settings = store.load()
    assert settings.archive_extraction_pool_gb == 2

    store.save(settings)
    saved = json.loads(store.settings_path.read_text(encoding="utf-8"))
    assert saved["archive_extraction_pool_gb"] == 2
    assert "archive_extraction_pool_mb" not in saved


def test_settings_archive_pool_uses_five_gb_default_and_one_to_fifty_range(tmp_path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    assert store.load().archive_extraction_pool_gb == 5

    viewmodel = SettingsViewModel(store.load(), store)
    viewmodel.set_archive_extraction_pool_gb(0)
    assert viewmodel.archive_extraction_pool_gb == 1
    viewmodel.set_archive_extraction_pool_gb(75)
    assert viewmodel.archive_extraction_pool_gb == 50
    assert store.load().archive_extraction_pool_gb == 50


def test_archive_pool_usage_can_display_soft_budget_overrun(qtbot) -> None:
    item = SettingsCacheStatusItem("Archive pool usage", int(6.2 * GIB), 5 * GIB)
    qtbot.addWidget(item)

    assert item.findChild(QLabel, "SettingsCacheUsageLabel").text() == "6.2 / 5 GB"


def test_main_window_opens_centered_floating_settings_overlay_and_restores_sidebar(qtbot) -> None:
    apply_theme()
    window = MainWindow(create_app_context())
    qtbot.addWidget(window)
    window.resize(Theme.window_width, Theme.window_height)
    window.show()
    QApplication.processEvents()

    window._handle_navigation("settings")
    QApplication.processEvents()

    selected_labels = [
        label.text()
        for item in window.sidebar.findChildren(QFrame)
        if item.property("selected") == "true"
        for label in item.findChildren(QLabel)
        if label.objectName() == "SidebarItemLabel"
    ]
    root = window.centralWidget()
    assert root is not None
    assert window.content_stack.currentWidget() is window.shelf_view
    assert window.settings_view.isVisible()
    assert window.settings_view.geometry().getRect() == (0, 0, root.width(), root.height())
    assert window.settings_view.page.geometry().center() == window.settings_view.rect().center()
    assert selected_labels == ["Settings"]
    assert all(not control.isHidden() for control in window.chrome._shelf_controls)

    qtbot.keyClick(window.settings_view, Qt.Key.Key_Escape)
    QApplication.processEvents()

    assert window.settings_view.isHidden()
    selected_labels = [
        label.text()
        for item in window.sidebar.findChildren(QFrame)
        if item.property("selected") == "true"
        for label in item.findChildren(QLabel)
        if label.objectName() == "SidebarItemLabel"
    ]
    assert selected_labels == ["All"]

    window._handle_navigation("settings")
    QApplication.processEvents()
    # Click below the title bar -- a press there is a blank-area dismiss
    # click, not window-drag intent (see the companion drag-handle test).
    qtbot.mouseClick(window.settings_view, Qt.MouseButton.LeftButton, pos=QPoint(5, Theme.toolbar_height + 20))
    QApplication.processEvents()

    assert window.settings_view.isHidden()


def test_main_window_settings_overlay_click_on_title_bar_moves_window_instead_of_closing(qtbot) -> None:
    """A press landing in the title bar's drag region is window-drag intent,
    not a dismiss click, even though the settings overlay covers it too."""

    apply_theme()
    window = MainWindow(create_app_context())
    qtbot.addWidget(window)
    window.resize(Theme.window_width, Theme.window_height)
    window.show()
    QApplication.processEvents()

    window._handle_navigation("settings")
    QApplication.processEvents()
    assert window.settings_view.isVisible()

    qtbot.mouseClick(window.settings_view, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    QApplication.processEvents()

    assert window.settings_view.isVisible()


def test_stylesheet_resolves_settings_tokens() -> None:
    stylesheet = ResourceLoader().load_stylesheet()

    assert "__SETTINGS_PANEL_BORDER_WIDTH__" not in stylesheet
    assert "__SETTINGS_SIDEBAR_RADIUS__" not in stylesheet
    assert "__SETTINGS_SWITCH_KNOB__" not in stylesheet
    assert "QFrame[class=\"SettingsPanel\"]" in stylesheet
    assert "QScrollArea#SettingsRightScrollArea QScrollBar:vertical" in stylesheet


# ---------------------------------------------------------------------------
# Privacy tab — Hidden Space rows


def test_privacy_tab_renders_show_collections_change_revert_and_reset_rows(qtbot) -> None:
    apply_theme()
    from joyread.ui.widgets.settings_page import SettingsButtonItem

    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.PRIVACY)
    QApplication.processEvents()

    button_items = page.findChildren(SettingsButtonItem)
    switch_items = page.findChildren(SettingsSwitchItem)
    labels = [
        label.text()
        for label in page.findChildren(QLabel)
        if label.property("class") == "SettingsItemNameText"
    ]

    assert any("Show Collections" in label for label in labels)
    assert {"Change Password", "Revert all", "Reset and Erase"}.issubset(set(labels))
    # Storage management rows live on the Privacy tab too. The Library Location
    # row shows the directory (address item); Select/Reset are button rows.
    assert {"Library Location", "Select Existing Library", "Reset Library"}.issubset(
        set(labels)
    )
    address_items = page.findChildren(SettingsAddressItem)
    assert len(address_items) == 1

    def _row_name(item: SettingsButtonItem) -> str:
        label = next(
            child
            for child in item.findChildren(QLabel)
            if child.property("class") == "SettingsItemNameText"
        )
        return label.text()

    by_name = {_row_name(item): item for item in button_items}
    # Three Hidden Space rows + two Storage button rows (Move is an address row).
    assert len(button_items) == 5
    # Hidden Space buttons are disabled until the feature is initialised; the
    # Storage buttons are always actionable.
    for hidden_label in ("Change Password", "Revert all", "Reset and Erase"):
        assert by_name[hidden_label].button.isEnabled() is False
    for storage_label in ("Select Existing Library", "Reset Library"):
        assert by_name[storage_label].button.isEnabled() is True
    # Two switches: Show Collections, and the encrypted-archive cache toggle.
    # Identified by label rather than by count so adding a third row does not
    # silently change which switch the tests below reach for.
    assert [_row_name(item) for item in switch_items] == [
        "Show Collections",
        "Delete cached pages when closing",
    ]


def test_privacy_show_collections_toggle_emits_setup_request_when_uninitialised(qtbot) -> None:
    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.PRIVACY)
    QApplication.processEvents()

    emitted: list[str] = []
    page.hidden_space_setup_requested.connect(lambda: emitted.append("setup"))
    page.hidden_space_verify_requested.connect(lambda: emitted.append("verify"))

    switch = page.findChildren(SettingsSwitchItem)[0].switch
    switch.set_checked(True)

    assert emitted == ["setup"]


def test_privacy_show_collections_toggle_off_persists_immediately(qtbot, tmp_path) -> None:
    apply_theme()
    from joyread.core.services.hidden_space_service import HiddenSpaceService
    from joyread.core.services.library_service import LibraryService
    from joyread.infrastructure.config.settings_store import SettingsStore
    from tests.support.in_memory_book_repository import InMemoryBookRepository

    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    settings = store.load()
    service = HiddenSpaceService(store, LibraryService(InMemoryBookRepository()))
    service.initialize("Pass1234", "Pass1234", None)
    settings = store.load()
    viewmodel = SettingsViewModel(settings, store, service)
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.PRIVACY)
    QApplication.processEvents()
    assert viewmodel.show_hidden_collection is True

    switch = page.findChildren(SettingsSwitchItem)[0].switch
    switch.set_checked(False)
    QApplication.processEvents()

    assert viewmodel.show_hidden_collection is False
    assert store.load().show_hidden_collection is False


def test_privacy_storage_rows_emit_move_select_reset(qtbot) -> None:
    apply_theme()
    from joyread.ui.widgets.settings_page import SettingsButtonItem, SettingsPushButton

    viewmodel = SettingsViewModel()
    viewmodel.set_storage_location("~/Documents/JoyRead-Library")
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.PRIVACY)
    QApplication.processEvents()

    emitted: list[str] = []
    page.storage_move_requested.connect(lambda: emitted.append("move"))
    page.storage_select_requested.connect(lambda: emitted.append("select"))
    page.storage_reset_requested.connect(lambda: emitted.append("reset"))

    # The Move row is an address item that displays the current directory.
    address_item = page.findChildren(SettingsAddressItem)[0]
    path_field = address_item.findChild(QLineEdit)
    assert path_field is not None
    assert path_field.text() == "~/Documents/JoyRead-Library"

    def _row_name(item: SettingsButtonItem) -> str:
        return next(
            child
            for child in item.findChildren(QLabel)
            if child.property("class") == "SettingsItemNameText"
        ).text()

    by_name = {_row_name(item): item for item in page.findChildren(SettingsButtonItem)}
    address_item.findChild(SettingsPushButton).click()
    by_name["Select Existing Library"].button.click()
    by_name["Reset Library"].button.click()

    assert emitted == ["move", "select", "reset"]


def test_storage_reset_requires_two_step_delete_confirmation(qtbot, monkeypatch) -> None:
    apply_theme()
    window = MainWindow(create_app_context())
    qtbot.addWidget(window)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        window.dialog_overlay,
        "show_confirm",
        lambda title, message, on_confirm, **kwargs: captured.update(
            confirm=on_confirm, destructive=kwargs.get("destructive")
        ),
    )
    monkeypatch.setattr(
        window.dialog_overlay,
        "show_input",
        lambda title, header, on_confirm, *, validator=None, **kwargs: captured.update(
            submit=on_confirm, validator=validator
        ),
    )
    executed: list[bool] = []
    monkeypatch.setattr(window, "_execute_reset_storage", lambda: executed.append(True))

    window._request_reset_storage()
    assert captured["destructive"] is True
    assert not executed  # first confirm does nothing destructive on its own

    captured["confirm"]()  # user presses Continue → second-step input appears
    validator = captured["validator"]
    assert validator("") is not None
    assert validator("remove") is not None
    assert validator("delete") is None
    assert validator("DELETE") is None
    assert validator("  delete  ") is None

    captured["submit"]("delete")  # typing the word and confirming runs the reset
    assert executed == [True]

    window._context.close()


def test_privacy_buttons_enable_once_hidden_space_initialised(qtbot, tmp_path) -> None:
    apply_theme()
    from joyread.core.services.hidden_space_service import HiddenSpaceService
    from joyread.core.services.library_service import LibraryService
    from joyread.infrastructure.config.settings_store import SettingsStore
    from joyread.ui.widgets.settings_page import SettingsButtonItem
    from tests.support.in_memory_book_repository import InMemoryBookRepository

    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    settings = store.load()
    service = HiddenSpaceService(store, LibraryService(InMemoryBookRepository()))
    service.initialize("Pass1234", "Pass1234", None)
    settings = store.load()
    viewmodel = SettingsViewModel(settings, store, service)
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.PRIVACY)
    QApplication.processEvents()

    button_items = page.findChildren(SettingsButtonItem)
    for item in button_items:
        assert item.button.isEnabled() is True


def test_encrypted_cache_switch_persists_and_defaults_on(qtbot, tmp_path) -> None:
    """Extracted pages of an encrypted archive are plaintext on disk, so the
    switch that bounds how long they live has to survive a restart. Defaults
    on: the pool is not encrypted yet, and one bulk-conversion pass per
    session is cheap enough that privacy is the better default."""

    apply_theme()
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    viewmodel = SettingsViewModel(store.load(), store)

    assert viewmodel.purge_encrypted_cache_on_close is True

    viewmodel.set_purge_encrypted_cache_on_close(False)

    assert store.load().purge_encrypted_cache_on_close is False
    assert SettingsViewModel(store.load(), store).purge_encrypted_cache_on_close is False


def _item_labels(page) -> list[str]:
    return [
        label.text()
        for label in page.findChildren(QLabel)
        if label.property("class") == "SettingsItemNameText"
    ]


def test_archive_scope_holds_the_archive_and_cache_groups(qtbot) -> None:
    """General had grown five banner groups against one each for Tags and
    Privacy. Archive and Cache are the two that are resource tuning rather than
    preferences, so they get their own scope."""

    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.ARCHIVE)
    QApplication.processEvents()

    banners = [
        label.text()
        for label in page.findChildren(QLabel, "SidebarSectionLabel")
    ]
    assert "Archive" in banners
    assert "Cache" in banners

    setting_items = [
        item for item in page.findChildren(QFrame) if item.property("class") == "SettingsItem"
    ]
    # Two shared depth rows, seven Archive rows, five Cache rows.
    assert len(setting_items) == 14


def test_the_shared_archive_depths_left_the_import_group(qtbot) -> None:
    """`nested_archive_max_depth` and `archive_global_file_max_depth` are read
    by the reader as well as by import (`reader_shell`, `reader_viewmodel`), so
    the Import banner was never their real scope."""

    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    QApplication.processEvents()

    general = _item_labels(page)
    assert "Nested archive depth" not in general
    assert "Archive global file depth" not in general
    # The one genuinely import-only setting stays behind.
    assert "Import folder depth" in general

    viewmodel.set_section(SettingsSectionKey.ARCHIVE)
    QApplication.processEvents()

    archive = _item_labels(page)
    assert "Nested archive depth" in archive
    assert "Archive global file depth" in archive
    assert "Import folder depth" not in archive


def test_every_sidebar_section_renders_something(qtbot) -> None:
    """A section whose dispatch branch is missing falls through to an empty
    list and renders a blank pane -- which looks like a layout bug, not a
    missing branch, and raises nothing."""

    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)

    for section in viewmodel.sections:
        if section.key == SettingsSectionKey.ABOUT:
            continue  # About has no content of its own yet.
        viewmodel.set_section(section.key)
        QApplication.processEvents()
        assert page._items_for_current_section(), f"{section.key} dispatched to nothing"


def test_the_archive_scope_is_labelled_and_ordered_after_general(qtbot) -> None:
    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    QApplication.processEvents()

    labels = [
        item.findChild(QLabel).text() for item in page.findChildren(SettingsSidebarItem)
    ]

    assert labels == ["General", "Archive & Cache", "Tags", "Privacy", "About"]


def test_the_gate_switches_are_bold_and_nothing_else_is(qtbot) -> None:
    """"Limit archive size" and "Resource guardrails" switch the rows beneath
    them on or off, so they are drawn as headings for the group they gate
    rather than as peers of it.

    Asserts the resolved font weight, not the QSS text: a selector that stops
    matching leaves the stylesheet valid and the rows quietly un-bolded.
    """

    apply_theme()
    viewmodel = SettingsViewModel()
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)
    viewmodel.set_section(SettingsSectionKey.ARCHIVE)
    QApplication.processEvents()

    weights = {
        label.text(): label.font().weight()
        for label in page.findChildren(QLabel)
        if label.property("class") == "SettingsItemNameText"
    }
    bold = {name for name, weight in weights.items() if weight >= QFont.Weight.Bold}

    assert bold == {"Limit archive size", "Resource guardrails"}
    # The rows they gate stay at the normal weight, or the hierarchy says nothing.
    assert weights["Maximum archive size"] < QFont.Weight.Bold
    assert weights["Maximum extracted item"] < QFont.Weight.Bold


def test_a_dropdown_is_wide_enough_for_its_longest_option(qtbot) -> None:
    """The design width fits "English" and "Zip bundle" but not every option.

    "Expensive and nested formats" is a *default*, so a fixed 121px rendered the
    control clipped at both ends the moment Settings opened. Measuring across
    every option rather than the current one also means picking a different
    value never resizes the control under the user's cursor.
    """

    apply_theme()
    page = SettingsPageWidget(SettingsViewModel(), ResourceLoader())
    qtbot.addWidget(page)
    page.resize(Theme.settings_panel_width, Theme.settings_panel_height)
    page.show()

    dropdowns = page.findChildren(SettingsDropdownButton)
    by_value = {dropdown.value: dropdown for dropdown in dropdowns}

    # Short options stay pixel-identical to the design.
    assert by_value["English"].width() == Theme.settings_dropdown_width

    policy = by_value["Expensive and nested formats"]
    metrics = QFontMetrics(policy.font())
    assert policy.width() > Theme.settings_dropdown_width
    assert policy.width() >= (
        metrics.horizontalAdvance("Expensive and nested formats")
        + Theme.settings_dropdown_indicator_width
    )


def test_the_conversion_policy_dropdown_is_translated_but_stores_an_enum() -> None:
    """Two separate things: what the user reads, and what gets written to disk.

    Translating the stored value would make a settings file unreadable after a
    language change; leaving the display in English makes a translated Settings
    panel look half-finished. So the enum's own string persists and only the
    label is localized — and a label chosen in one language still resolves after
    switching to another.
    """

    viewmodel = SettingsViewModel()
    labels_by_language = {}
    for language in ("English", "Japanese", "Chinese"):
        locale_service.load_language(language)
        options = viewmodel.canonical_import_policy_options
        assert len(options) == 3
        assert all(option and not option.startswith("settings.") for option in options)
        labels_by_language[language] = options

    assert len(set(labels_by_language.values())) == 3  # genuinely different text

    # Picking the Japanese label for "always" stores the enum value, not the text.
    locale_service.load_language("Japanese")
    viewmodel.set_canonical_import_policy(labels_by_language["Japanese"][2])
    assert viewmodel.canonical_import_policy.value == "always"

    locale_service.load_language("English")
    assert viewmodel.canonical_import_policy_label == "Always"

    # And a value read back from settings still resolves, whatever the language.
    locale_service.load_language("Chinese")
    viewmodel.set_canonical_import_policy("expensive_and_nested")
    assert viewmodel.canonical_import_policy.value == "expensive_and_nested"
    locale_service.load_language("English")
