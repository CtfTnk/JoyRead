from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QToolButton

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import FileFilter, SortField
from joyread.ui.widgets.dropdown_button import FigmaDropdownButton
from joyread.ui.widgets.search_panel import SearchPanelWidget


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
    assert panel.width() == Theme.search_panel_width

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
