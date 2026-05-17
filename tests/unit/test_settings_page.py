from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QToolButton, QWidget

from joyread.app.app_context import create_app_context
from joyread.core.models.cache import ArchiveCacheStrategy
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsSectionKey, SettingsViewModel
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.settings_view import SettingsView
from joyread.ui.widgets.settings_page import (
    SettingsAddressItem,
    SettingsContentPanel,
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
    viewmodel.set_language("English")
    viewmodel.set_storage_location("~/Documents/JoyRead Library")
    viewmodel.set_archive_cache_strategy("Hidden image files")
    viewmodel.set_import_folder_max_depth(3)
    viewmodel.set_archive_internal_max_depth(4)

    assert viewmodel.current_section == SettingsSectionKey.TAGS
    assert viewmodel.import_book_when_opening is True
    assert viewmodel.individual_read_window is True
    assert viewmodel.storage_location == "~/Documents/JoyRead Library"
    assert viewmodel.archive_cache_strategy == ArchiveCacheStrategy.HIDDEN_IMAGE_FILES
    assert viewmodel.archive_cache_strategy_label == "Hidden image files"
    assert viewmodel.import_folder_max_depth == 3
    assert viewmodel.archive_internal_max_depth == 4
    assert len(changes) == 7


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
    assert sidebar_item_positions["Tags"] - sidebar_item_positions["General"] == (
        Theme.settings_sidebar_item_height + Theme.settings_sidebar_gap
    )
    assert sidebar_item_positions["Privacy"] - sidebar_item_positions["Tags"] == (
        Theme.settings_sidebar_item_height + Theme.settings_sidebar_gap
    )
    assert sidebar_item_positions["About"] > Theme.settings_panel_height - 80
    # Four original General rows, two Import depth rows, and five Cache rows.
    assert len(setting_items) == 11
    spin_buttons = page.findChildren(SettingsSpinButtonSmall)
    assert len(spin_buttons) == 5
    assert {spin.size().width() for spin in spin_buttons} == {Theme.settings_spin_width}
    assert {spin.size().height() for spin in spin_buttons} == {Theme.settings_spin_height}


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

    qtbot.mouseClick(switch, Qt.MouseButton.LeftButton)

    assert switch.checked is True
    assert emitted == [True]
    assert knob.x() == switch.width() - Theme.settings_switch_layout_margin - Theme.settings_switch_knob_size


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
    qtbot.mouseClick(window.settings_view, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    QApplication.processEvents()

    assert window.settings_view.isHidden()


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
    # Three button rows.
    assert len(button_items) == 3
    # All three buttons disabled until the feature is initialised.
    for item in button_items:
        assert item.button.isEnabled() is False
    # The Show Collections switch is the only switch on this tab.
    assert len(switch_items) == 1


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
