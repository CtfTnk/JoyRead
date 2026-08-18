from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QToolButton

from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import FileFilter, SortField
from joyread.ui.widgets.dropdown_button import FigmaDropdownButton
from joyread.ui.widgets.mode_switches import SortModeSwitchWidget
from joyread.ui.widgets.search_panel import SearchPanelWidget
from joyread.ui.widgets.top_toolbar import TagFilterButton, TopToolbarWidget
from joyread.ui.widgets.window_chrome import (
    ActionMenuButton,
    StoplightControlsWidget,
    TitleBarWidget,
    TitleControlButton,
    TitleControlGroup,
)


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def test_figma_dropdown_button_has_fixed_figma_size_and_value_signal(qtbot) -> None:
    apply_theme()
    button = FigmaDropdownButton(
        ResourceLoader(),
        [field.value for field in SortField],
        width=Theme.sort_dropdown_width,
        initial_value=SortField.ADD_TIME.value,
        tooltip="Sort by",
    )
    qtbot.addWidget(button)

    emitted: list[str] = []
    button.value_changed.connect(emitted.append)

    assert button.width() == Theme.sort_dropdown_width
    assert button.height() == Theme.toolbar_control_height
    effect = button.graphicsEffect()
    assert effect is not None
    assert effect.offset().x() == 0
    assert effect.offset().y() == 1

    inner_button = button.findChild(QFrame, "FigmaDropdownInnerButton")
    assert inner_button is not None
    assert inner_button.property("hovered") == "false"

    QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
    assert inner_button.property("hovered") == "true"
    QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    assert inner_button.property("hovered") == "false"

    button.set_value(SortField.TITLE.value, emit=True)
    button.set_value(SortField.TITLE.value, emit=True)

    assert button.value == SortField.TITLE.value
    assert emitted == [SortField.TITLE.value]


def test_search_panel_collapses_expands_and_submits_only_on_action(qtbot) -> None:
    apply_theme()
    panel = SearchPanelWidget(ResourceLoader())
    qtbot.addWidget(panel)
    panel.show()
    QApplication.processEvents()

    emitted: list[str] = []
    panel.search_submitted.connect(emitted.append)

    search_input = panel.findChild(QLineEdit, "FigmaSearchInput")
    collapse_button = panel.findChild(QToolButton, "CollapseSearchButton")
    expand_button = panel.findChild(QToolButton, "ExpandSearchButton")
    submit_button = panel.findChild(QToolButton, "SearchSubmitButton")

    assert search_input is not None
    assert collapse_button is not None
    assert expand_button is not None
    assert submit_button is not None
    assert search_input.placeholderText() == "Search books..."
    assert panel.width() == Theme.toolbar_button_size

    search_bar = panel.findChild(QFrame, "FigmaSearchBar")
    assert search_bar is not None
    assert search_bar.layout().spacing() == Theme.search_bar_gap
    assert search_input.minimumWidth() == Theme.search_input_text_width
    assert search_input.alignment() & Qt.AlignmentFlag.AlignLeft

    search_input.setText("spy")
    QApplication.processEvents()
    assert emitted == []

    qtbot.mouseClick(submit_button, Qt.MouseButton.LeftButton)
    assert emitted == ["spy"]

    search_input.setText("family")
    search_input.returnPressed.emit()
    assert emitted == ["spy", "family"]

    qtbot.mouseClick(collapse_button, Qt.MouseButton.LeftButton)
    assert panel.width() == Theme.toolbar_button_size
    assert expand_button.isVisible()

    qtbot.mouseClick(expand_button, Qt.MouseButton.LeftButton)
    assert panel.width() == Theme.search_panel_width
    assert collapse_button.isVisible()


def test_file_filter_values_follow_figma_extension_options() -> None:
    assert [filter_name.value for filter_name in FileFilter] == [
        "ALL",
        "CBZ",
        "CBR",
        "ZIP",
        "RAR",
        "7Z",
        "PDF",
        "EPUB",
    ]


def test_tag_filter_button_loads_inactive_icon_on_construction(qtbot) -> None:
    apply_theme()
    button = TagFilterButton(ResourceLoader())
    qtbot.addWidget(button)

    icon_label = button.findChild(QLabel, "TagFilterButtonIcon")

    assert button.active is False
    assert button.property("active") == "false"
    assert icon_label is not None
    pixmap = icon_label.pixmap()
    assert pixmap is not None
    assert not pixmap.isNull()


def test_top_toolbar_tag_filter_button_sits_after_file_filter_and_swaps_state(qtbot) -> None:
    apply_theme()
    toolbar = TopToolbarWidget(ResourceLoader())
    qtbot.addWidget(toolbar)
    toolbar.show()
    QApplication.processEvents()

    emitted: list[None] = []
    toolbar.tag_filter_requested.connect(lambda: emitted.append(None))
    tag_button = toolbar.findChild(TagFilterButton)
    dropdowns = toolbar.findChildren(FigmaDropdownButton)

    assert tag_button is not None
    assert dropdowns
    assert tag_button.x() > dropdowns[-1].x()
    assert tag_button.size().width() == Theme.toolbar_button_size
    assert tag_button.property("active") == "false"

    toolbar.set_tag_filter_active(True)
    assert tag_button.active is True
    assert tag_button.property("active") == "true"

    qtbot.mouseClick(tag_button, Qt.MouseButton.LeftButton)
    assert emitted == [None]


def test_action_menu_button_shadow_is_shifted_down(qtbot) -> None:
    apply_theme()
    button = ActionMenuButton(ResourceLoader())
    qtbot.addWidget(button)

    effect = button.graphicsEffect()

    assert effect is not None
    assert effect.offset().x() == 0
    assert effect.offset().y() == 1


def test_title_control_group_matches_figma_geometry_after_sort_switch(qtbot) -> None:
    apply_theme()
    title_bar = TitleBarWidget(ResourceLoader(), platform_name="win32")
    qtbot.addWidget(title_bar)
    title_bar.resize(720, Theme.toolbar_height)
    title_bar.show()
    QApplication.processEvents()

    group = title_bar.findChild(TitleControlGroup, "TitleControlGroup")
    sort_switch = title_bar.findChild(SortModeSwitchWidget)
    assert group is not None
    assert sort_switch is not None

    buttons = group.findChildren(TitleControlButton)
    margins = group.layout().contentsMargins()

    assert group.isVisible()
    assert group.width() == Theme.title_control_group_width
    assert group.height() == Theme.title_control_group_height
    assert group.x() - (sort_switch.x() + sort_switch.width()) == 10
    assert group.layout().spacing() == Theme.title_control_gap
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
        0,
        0,
        Theme.title_control_group_right_inset,
        0,
    )
    assert len(buttons) == 3
    assert {button.width() for button in buttons} == {Theme.title_control_button_size}
    assert {button.height() for button in buttons} == {Theme.title_control_button_size}
    assert buttons[1].x() - (buttons[0].x() + buttons[0].width()) == Theme.title_control_gap
    assert buttons[2].x() - (buttons[1].x() + buttons[1].width()) == Theme.title_control_gap
    assert group.width() - (buttons[2].x() + buttons[2].width()) == Theme.title_control_group_right_inset


def test_title_bar_refresh_labels_translates_sort_display_and_keeps_canonical_signal(qtbot) -> None:
    apply_theme()
    title_bar = TitleBarWidget(ResourceLoader(), platform_name="win32")
    qtbot.addWidget(title_bar)
    title_bar.show()
    QApplication.processEvents()

    emitted: list[tuple[str, bool]] = []
    title_bar.sort_changed.connect(lambda field, ascending: emitted.append((field, ascending)))
    sort_dropdown = title_bar.findChildren(FigmaDropdownButton)[0]
    action_button = title_bar.findChild(ActionMenuButton)

    locale_service.load_language("Chinese")
    title_bar.refresh_labels()

    assert sort_dropdown.value == "添加时间"
    assert sort_dropdown.toolTip() == "排序方式"
    assert action_button is not None
    assert action_button.toolTip() == "操作"

    sort_dropdown.set_value("书名", emit=True)

    assert emitted == [(SortField.TITLE.value, False)]
    locale_service.load_language("English")


def test_title_bar_switches_between_macos_and_forced_non_macos_controls(qtbot) -> None:
    apply_theme()
    title_bar = TitleBarWidget(ResourceLoader(), platform_name="darwin")
    qtbot.addWidget(title_bar)
    title_bar.resize(720, Theme.toolbar_height)
    title_bar.show()
    QApplication.processEvents()

    stoplights = title_bar.findChild(StoplightControlsWidget)
    group = title_bar.findChild(TitleControlGroup, "TitleControlGroup")

    assert stoplights is not None
    assert group is not None
    assert stoplights.isVisible()
    assert group.isHidden()

    title_bar.set_title_control_mode(
        force_non_macos_title_controls=True,
    )
    QApplication.processEvents()

    assert stoplights.isHidden()
    assert group.isVisible()

    title_bar.set_title_control_mode(
        force_non_macos_title_controls=False,
    )
    QApplication.processEvents()

    assert stoplights.isVisible()
    assert group.isHidden()


def test_title_control_group_emits_window_action_signals(qtbot) -> None:
    apply_theme()
    group = TitleControlGroup(ResourceLoader())
    qtbot.addWidget(group)
    group.show()
    QApplication.processEvents()

    emitted: list[str] = []
    group.minimize_requested.connect(lambda: emitted.append("minimize"))
    group.zoom_requested.connect(lambda: emitted.append("zoom"))
    group.close_requested.connect(lambda: emitted.append("close"))

    qtbot.mouseClick(group.minimize_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(group.zoom_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(group.close_button, Qt.MouseButton.LeftButton)

    assert emitted == ["minimize", "zoom", "close"]
