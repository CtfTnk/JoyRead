from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QWidget

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.menus import FigmaMenu, build_action_menu, build_book_context_menu


def make_book() -> Book:
    now = datetime(2026, 1, 1)
    return Book(
        uuid="book-1",
        title="Book",
        author="Author",
        language_tag="en",
        book_type="Comic",
        file_format="CBZ",
        file_path="/tmp/book.cbz",
        progress=0.5,
        cover_thumbnail_path=None,
        added_at=now,
        updated_at=now,
        last_read_at=None,
        is_favourite=False,
    )


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def menu_rows(menu: QWidget) -> list[QFrame]:
    return [widget for widget in menu.findChildren(QFrame) if widget.objectName() == "FigmaMenuItem"]


def assert_figma_menu_layout(menu: QWidget, labels: list[str], expected_width: int = Theme.menu_width) -> None:
    menu.show()
    QApplication.processEvents()

    panel = menu.findChild(QFrame, "FigmaMenuPanel")
    option_list = menu.findChild(QWidget, "FigmaMenuOptionList")
    rows = menu_rows(menu)

    assert [row.findChild(QLabel).text() for row in rows] == labels
    assert menu.width() == expected_width
    assert panel.width() == expected_width

    panel_margins = panel.layout().contentsMargins()
    assert (
        panel_margins.left(),
        panel_margins.top(),
        panel_margins.right(),
        panel_margins.bottom(),
    ) == (
        Theme.menu_layout_margin,
        Theme.menu_layout_margin,
        Theme.menu_layout_margin,
        Theme.menu_layout_margin,
    )

    assert option_list.geometry().x() == Theme.menu_visual_padding
    assert option_list.geometry().y() == Theme.menu_visual_padding

    option_margins = option_list.layout().contentsMargins()
    assert (option_margins.left(), option_margins.top(), option_margins.right(), option_margins.bottom()) == (
        0,
        0,
        0,
        0,
    )
    assert option_list.layout().spacing() == Theme.menu_option_gap

    gaps = [
        rows[index + 1].geometry().y() - rows[index].geometry().bottom() - 1
        for index in range(len(rows) - 1)
    ]
    assert gaps == [Theme.menu_option_gap] * (len(rows) - 1)

    menu.close()


def test_action_menu_uses_figma_panel_and_option_list(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)

    menu = build_action_menu(parent, lambda: None, lambda: None, lambda: None)
    qtbot.addWidget(menu)

    assert_figma_menu_layout(menu, ["Open Book", "Open & Import", "Import"])


def test_figma_menu_can_match_trigger_width(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)

    menu = FigmaMenu(parent, width=Theme.file_filter_width)
    menu.add_item("ALL", lambda: None)
    qtbot.addWidget(menu)

    assert_figma_menu_layout(menu, ["ALL"], expected_width=Theme.file_filter_width)


def test_menu_item_triggers_after_mouse_release(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)

    triggered: list[str] = []
    menu = FigmaMenu(parent)
    menu.add_item("Read", lambda: triggered.append("read"))
    qtbot.addWidget(menu)
    menu.show()
    QApplication.processEvents()

    row = menu_rows(menu)[0]
    qtbot.mousePress(row, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert triggered == []

    qtbot.mouseRelease(row, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert triggered == ["read"]


def test_book_context_menu_uses_figma_panel_and_option_list(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)

    menu = build_book_context_menu(
        parent,
        make_book(),
        on_read=lambda _uuid: None,
        on_favourite=lambda _uuid: None,
        on_detail=lambda _uuid: None,
        on_add_to_collection=lambda _uuid: None,
        on_remove=lambda _uuid: None,
    )
    qtbot.addWidget(menu)

    assert_figma_menu_layout(menu, ["Read", "Favourite", "Detail", "Add to...", "Remove", "Delete"])
    delete_row = menu_rows(menu)[-1]
    assert delete_row.property("destructive") == "true"
    assert delete_row.property("menuEnabled") == "false"


def test_book_context_menu_can_hide_remove_action(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)

    menu = build_book_context_menu(
        parent,
        make_book(),
        on_read=lambda _uuid: None,
        on_favourite=lambda _uuid: None,
        on_detail=lambda _uuid: None,
        on_add_to_collection=lambda _uuid: None,
        on_remove=lambda _uuid: None,
        show_remove=False,
    )
    qtbot.addWidget(menu)

    assert_figma_menu_layout(menu, ["Read", "Favourite", "Detail", "Add to...", "Delete"])
