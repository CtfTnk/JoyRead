"""Material feedback preserves text allocation, ownership and virtual grids."""

import pytest
from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.reader_items import ReaderContentsItem
from joyread.ui.widgets.book_detail import BookDetailPanel, DetailThumbnailGrid
from joyread.ui.widgets.menus import FigmaMenu, MenuItem
from joyread.ui.widgets.reader_controls import ReaderHeader, reader_button
from joyread.ui.widgets.reader_topic_panel import _NewBookmarkRow, _TopicContentsRow
from joyread.ui.widgets.translucent_material import glass_profile
from tests.unit.test_thumbnail_grid_cache import image_bytes
from tests.support.in_memory_book_repository import InMemoryBookRepository


@pytest.fixture
def resources(qapp):
    resources = ResourceLoader()
    for path in resources.font_paths():
        QFontDatabase.addApplicationFont(str(path))
    qapp.setStyleSheet(resources.load_stylesheet())
    return resources


def pixel(widget, x, y):
    # Virtual grids can be millions of pixels tall. Render only this probe,
    # never the entire scroll extent just to inspect one material pixel.
    return widget.grab(QRect(x, y, 1, 1)).toImage().pixelColor(0, 0)


def assert_color(actual, expected):
    # Premultiplied 8-bit rendering can round a translucent RGB channel by 1.
    assert max(abs(a - b) for a, b in zip(actual.getRgb(), expected.getRgb(), strict=True)) <= 1


@pytest.mark.parametrize("profile", ("quiet", "contrast"))
@pytest.mark.parametrize("kind", ("menu", "contents", "new_bookmark"))
def test_rows_paint_inside_their_text_allocation(qtbot, resources, profile, kind):
    if kind == "menu":
        row = MenuItem("Read")
    elif kind == "contents":
        row = _TopicContentsRow(ReaderContentsItem("Chapter one", 3, 0))
    else:
        row = _NewBookmarkRow(resources)
    row.setProperty("glassBackdrop", profile)
    qtbot.addWidget(row)
    row.resize(300, row.height())
    row.show()
    QApplication.processEvents()
    QApplication.sendEvent(row, QEvent(QEvent.Type.Leave))
    assert pixel(row, 150, 2).alpha() == 0
    before = row.geometry(), row.contentsRect(), row.layout().contentsMargins()
    QApplication.sendEvent(row, QEvent(QEvent.Type.Enter))
    palette = Theme.glass_palettes[profile]
    assert pixel(row, 150, 2) == QColor(*palette.hover)
    assert pixel(row, 150, 0).lightness() < 240  # a fine edge, not a dark fill
    qtbot.mousePress(row, Qt.MouseButton.LeftButton)
    assert_color(pixel(row, 150, 2), QColor(*palette.pressed))
    qtbot.mouseRelease(row, Qt.MouseButton.LeftButton)
    assert (row.geometry(), row.contentsRect(), row.layout().contentsMargins()) == before
    assert row.height() == (19 if kind == "menu" else 24)
    for label in row.findChildren(QLabel):
        assert label.height() >= QFontMetrics(label.font()).height()
        assert row.rect().contains(label.geometry())


def test_selected_and_disabled_menu_rows_retain_their_action_semantics(qtbot, resources):
    row = MenuItem("English", selected=True)
    disabled = MenuItem("Unavailable", enabled=False)
    qtbot.addWidget(row)
    qtbot.addWidget(disabled)
    for widget in (row, disabled):
        widget.resize(200, 19)
        widget.show()
        QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert pixel(row, 150, 2) == QColor(*Theme.glass_palettes["quiet"].selected)
    triggered = []
    disabled.clicked.connect(lambda: triggered.append(True))
    qtbot.mouseClick(disabled, Qt.MouseButton.LeftButton)
    assert pixel(disabled, 150, 2).alpha() == 0
    QApplication.processEvents()
    assert not triggered


def test_embedded_reader_popup_uses_nearest_backdrop_and_preserves_inset(qtbot, resources):
    library = QWidget()
    library.setProperty("glassBackdrop", "quiet")
    reader = QWidget(library)
    reader.setProperty("glassBackdrop", "contrast")
    anchor = QWidget(reader)
    qtbot.addWidget(library)
    for owner, profile in ((library, "quiet"), (anchor, "contrast")):
        menu = FigmaMenu(owner)
        qtbot.addWidget(menu)
        menu.add_item("Read", None)
        menu.show()
        QApplication.processEvents()
        assert glass_profile(menu) == profile
        # Capture the popup, so the panel is composited once rather than
        # grabbing a child on top of its already-painted parent background.
        assert pixel(menu, 10, 3) == QColor(*Theme.glass_palettes[profile].panel)
        assert menu._option_list.mapTo(menu._panel, QPoint()).toTuple() == (6, 6)
        menu.close()


def test_reader_button_keeps_icon_size_when_material_changes(qtbot, resources):
    header = ReaderHeader(resources)
    qtbot.addWidget(header)
    assert glass_profile(header) == "contrast"
    host = QWidget()
    host.setProperty("glassBackdrop", "contrast")
    host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    qtbot.addWidget(host)
    button = reader_button(resources, "icon_bookmark.svg", "Bookmark")
    button.setParent(host)
    host.show()
    button.show()
    QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    allocation = button.size(), button.iconSize(), button.contentsRect()
    assert button.size().toTuple() == (36, 36)
    assert button.iconSize().toTuple() == (24, 24)
    assert pixel(host, 18, 3) == QColor(*Theme.glass_palettes["contrast"].idle)
    qtbot.mouseMove(button)
    assert pixel(host, 18, 3) == QColor(*Theme.glass_palettes["contrast"].hover)
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    assert pixel(host, 18, 3) == QColor(*Theme.glass_palettes["contrast"].pressed)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    assert (button.size(), button.iconSize(), button.contentsRect()) == allocation


def test_thumbnail_feedback_covers_image_and_number_without_page_widgets(qtbot, resources):
    grid = DetailThumbnailGrid(interaction_feedback=True)
    qtbot.addWidget(grid)
    grid.resize(400, 500)
    grid.set_thumbnail_count(100_000)
    grid.show()
    assert not grid.findChildren(QWidget)
    selected = []
    grid.thumbnail_clicked.connect(selected.append)
    qtbot.mouseMove(grid, grid._index_rect(0).center())
    assert grid._feedback_index == 0
    assert grid._selection_rect(0).contains(grid._item_rect(0))
    assert grid._selection_rect(0).contains(grid._index_rect(0))
    assert_color(pixel(grid, grid._selection_rect(0).left() + 2, grid._item_rect(0).center().y()),
                 QColor(*Theme.glass_palettes["quiet"].hover))
    qtbot.mouseClick(grid, Qt.MouseButton.LeftButton, pos=grid._index_rect(0).center())
    grid.set_interest((0,))
    grid.set_thumbnail(0, image_bytes())
    child = grid._thumbnails[0]
    original_image = pixel(grid, child.geometry().center().x(), child.geometry().center().y())
    QApplication.sendEvent(child, QEvent(QEvent.Type.Enter))
    qtbot.mousePress(child, Qt.MouseButton.LeftButton)
    assert grid._feedback_pressed and grid._feedback_index == 0
    assert pixel(grid, child.geometry().center().x(), child.geometry().center().y()) == original_image == QColor("red")
    qtbot.mouseRelease(child, Qt.MouseButton.LeftButton)
    assert not grid._feedback_pressed and selected == [0, 0]
    assert grid.findChildren(QWidget) == [child]
    qtbot.mouseMove(grid, QPoint(1, 1))
    assert grid._feedback_index is None
    qtbot.mouseMove(grid, grid._item_rect(1).center())
    assert grid._feedback_index == 1
    grid.reset_unknown()
    assert grid._feedback_index is None
    assert not grid._thumbnails and not grid._retained


def test_thumbnail_selection_dismissal_clears_feedback_before_next_open(qtbot, resources):
    grid = DetailThumbnailGrid(interaction_feedback=True)
    qtbot.addWidget(grid)
    grid.set_thumbnail_count(10)
    grid.show()
    grid.thumbnail_clicked.connect(lambda _index: grid.hide())
    qtbot.mouseClick(grid, Qt.MouseButton.LeftButton, pos=grid._item_rect(0).center())
    assert grid.isHidden() and grid._feedback_index is None
    grid.show()
    assert grid._feedback_index is None


def test_detail_panel_thumbnail_feedback_works_for_loaded_images_and_page_labels(qtbot, resources):
    panel = BookDetailPanel(resources)
    qtbot.addWidget(panel)
    book = InMemoryBookRepository().list_books()[0]
    panel.set_book(book)
    panel.set_thumbnail_page_count(book.uuid, 80)
    panel.resize(400, 380)
    panel.show()
    QApplication.processEvents()
    grid = panel._thumbnail_grid
    panel._scroll.verticalScrollBar().setValue(grid.y())
    QApplication.processEvents()
    panel.set_page_thumbnail(book.uuid, 0, image_bytes())
    calls = []
    panel.read_at_index_requested.connect(lambda uuid, index: calls.append((uuid, index)))

    # The real panel must enable the shared feedback, including events sent to
    # opaque image children instead of just the virtual grid's page labels.
    qtbot.mouseMove(grid, grid._index_rect(0).center())
    assert grid._feedback_index == 0
    qtbot.mousePress(grid, Qt.MouseButton.LeftButton, pos=grid._index_rect(0).center())
    assert grid._feedback_pressed
    qtbot.mouseRelease(grid, Qt.MouseButton.LeftButton, pos=grid._index_rect(0).center())
    assert not grid._feedback_pressed
    child = grid._thumbnails[0]
    qtbot.mouseMove(child)
    assert grid._feedback_index == 0
    qtbot.mousePress(child, Qt.MouseButton.LeftButton)
    assert grid._feedback_pressed
    assert pixel(grid, child.geometry().center().x(), child.geometry().center().y()) == QColor("red")
    qtbot.mouseRelease(child, Qt.MouseButton.LeftButton)
    assert not grid._feedback_pressed
    assert calls == [(book.uuid, 0), (book.uuid, 0)]
    panel.hide()
    assert grid._feedback_index is None and not grid._feedback_pressed
