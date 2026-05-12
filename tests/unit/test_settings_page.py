from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QWidget

from joyread.app.app_context import create_app_context
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

    assert viewmodel.current_section == SettingsSectionKey.TAGS
    assert viewmodel.import_book_when_opening is True
    assert viewmodel.individual_read_window is True
    assert viewmodel.storage_location == "~/Documents/JoyRead Library"
    assert len(changes) == 4


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
    assert sidebar_item_positions["Private Space"] - sidebar_item_positions["Tags"] == (
        Theme.settings_sidebar_item_height + Theme.settings_sidebar_gap
    )
    assert sidebar_item_positions["About"] > Theme.settings_panel_height - 80
    # Four original General rows (Language, Import switch, Window switch,
    # Storage Location) plus the new Cache sub-group: three numeric inputs
    # and one usage/clear row = eight QFrames flagged ``class=SettingsItem``.
    assert len(setting_items) == 8


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
