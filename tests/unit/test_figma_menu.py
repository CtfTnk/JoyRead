from datetime import datetime

from PySide6.QtCore import QEventLoop, QPoint, QTimer, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QScrollArea, QWidget

from joyread.core.models.book import Book
from joyread.core.models.language import Language
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.menus import (
    FigmaMenu,
    build_action_menu,
    build_book_context_menu,
    build_language_dropdown_menu,
)


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
    # MenuItem defers ``clicked``, then _trigger defers the callback: two turns.
    QApplication.processEvents()
    QApplication.processEvents()
    assert triggered == ["read"]


def test_book_context_menu_uses_figma_panel_and_option_list(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)
    deleted: list[str] = []

    menu = build_book_context_menu(
        parent,
        make_book(),
        on_read=lambda _uuid: None,
        on_favourite=lambda _uuid: None,
        on_detail=lambda _uuid: None,
        on_add_to_collection=lambda _uuid: None,
        on_export=lambda _uuid: None,
        on_remove=lambda _uuid: None,
        on_delete=deleted.append,
    )
    qtbot.addWidget(menu)

    assert_figma_menu_layout(menu, ["Read", "Favourite", "Detail", "Add to...", "Export", "Remove", "Delete"])
    delete_row = menu_rows(menu)[-1]
    assert delete_row.property("destructive") == "true"
    assert delete_row.property("menuEnabled") == "true"
    menu.show()
    QApplication.processEvents()
    qtbot.mousePress(delete_row, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(delete_row, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    QApplication.processEvents()
    assert deleted == ["book-1"]


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
        on_export=lambda _uuid: None,
        on_remove=lambda _uuid: None,
        on_delete=lambda _uuid: None,
        show_remove=False,
    )
    qtbot.addWidget(menu)

    assert_figma_menu_layout(menu, ["Read", "Favourite", "Detail", "Add to...", "Export", "Delete"])


def test_language_dropdown_menu_matches_figma_structure_and_selection(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)
    selected: list[str] = []
    languages = (
        Language("English", "en"),
        Language("Chinese", "zh"),
        Language("Japanese", "ja"),
        Language("Unknown", "und"),
    )

    menu = build_language_dropdown_menu(parent, ResourceLoader(), languages, "en", selected.append)
    qtbot.addWidget(menu)
    menu.show()
    QApplication.processEvents()

    panel = menu.findChild(QFrame, "LanguageDropdownMenuPanel")
    indicators = menu.findChildren(QWidget, "LanguageDropdownMenuIndicator")
    scroll_area = menu.findChild(QScrollArea, "LanguageDropdownMenuScrollArea")
    rows = menu_rows(menu)
    panel_margins = panel.layout().contentsMargins()

    assert menu.width() == Theme.language_menu_width
    assert [row.findChild(QLabel).text() for row in rows] == ["English", "Chinese", "Japanese", "Unknown"]
    assert [indicator.property("direction") for indicator in indicators] == ["up", "down"]
    assert rows[0].property("selected") == "true"
    assert rows[1].property("selected") == "false"
    assert (
        panel_margins.left(),
        panel_margins.top(),
        panel_margins.right(),
        panel_margins.bottom(),
    ) == (
        Theme.language_menu_layout_margin_horizontal,
        Theme.language_menu_layout_margin_vertical,
        Theme.language_menu_layout_margin_horizontal,
        Theme.language_menu_layout_margin_vertical,
    )
    assert scroll_area is not None
    assert scroll_area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll_area.height() == (4 * Theme.menu_item_height) + (3 * Theme.menu_option_gap)

    qtbot.mousePress(rows[2], Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(rows[2], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    QApplication.processEvents()

    assert selected == ["ja"]


def test_language_dropdown_scrolls_after_seven_items_without_scrollbar(qtbot) -> None:
    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)
    languages = tuple(Language(f"Language {index}", f"x{index}") for index in range(8))

    menu = build_language_dropdown_menu(parent, ResourceLoader(), languages, "x0", lambda _code: None)
    qtbot.addWidget(menu)
    menu.show()
    QApplication.processEvents()

    scroll_area = menu.findChild(QScrollArea, "LanguageDropdownMenuScrollArea")
    option_list = menu.findChild(QWidget, "LanguageDropdownMenuOptionList")

    assert scroll_area is not None
    assert option_list is not None
    assert scroll_area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll_area.height() == (7 * Theme.menu_item_height) + (6 * Theme.menu_option_gap)
    assert option_list.height() > scroll_area.height()


def test_menu_event_loop_is_not_parented_to_the_menu(qtbot) -> None:
    """A menu's event loop must outlive the menu, not belong to it.

    ``QEventLoop(self)`` is destroyed with its parent widget, and a menu is
    routinely destroyed by the action it just triggered -- closing the window
    the menu belongs to. The loop object then dies while its ``exec()`` is
    still on the stack, leaving a dangling pointer in
    ``QThreadData::eventLoops``. Nothing fails at that moment: the crash comes
    later, when ``QCoreApplication::exit()`` walks that list and calls
    ``exit()`` on every entry, which is why it surfaces as a segfault on quit
    with no menu anywhere in the backtrace.
    """

    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)
    menu = FigmaMenu(parent)
    qtbot.addWidget(menu)
    seen: list[QEventLoop | None] = []

    def capture() -> None:
        seen.append(menu._loop)
        menu.close()

    QTimer.singleShot(0, capture)
    menu.exec(QPoint(0, 0))

    assert seen, "the menu's event loop never ran"
    loop = seen[0]
    assert loop is not None
    assert loop.parent() is None, "the event loop must not be owned by the menu"


def test_menu_action_does_not_run_inside_the_menu_event_loop(qtbot) -> None:
    """The triggered action must run after ``exec()`` returns, not under it.

    ``close()`` only asks the loop to quit and returns before ``exec()`` does,
    so invoking the callback straight after it runs the whole action nested
    inside the menu's own loop. Every menu-driven step then stacks another
    event loop on the main thread -- the reported crash had five -- and the
    menu cannot be torn down while its own ``exec()`` is still running.
    """

    apply_theme()
    parent = QWidget()
    qtbot.addWidget(parent)
    menu = FigmaMenu(parent)
    qtbot.addWidget(menu)
    # ``isRunning()`` is useless as a probe here: ``close()`` calls ``quit()``
    # first, which clears it while ``exec()`` is still on the stack. Observing
    # whether ``exec()`` has *returned* is the real question.
    exec_returned: list[bool] = []
    observed: list[bool] = []

    def action() -> None:
        observed.append(bool(exec_returned))

    QTimer.singleShot(0, lambda: menu._trigger(action))
    menu.exec(QPoint(0, 0))
    exec_returned.append(True)
    QApplication.processEvents()
    QApplication.processEvents()

    assert observed == [True], "the action ran before the menu's exec() returned"
