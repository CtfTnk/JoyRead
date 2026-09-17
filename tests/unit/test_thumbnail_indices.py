from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtCore import QEvent
from PySide6.QtGui import QFontMetrics, QMouseEvent
from PySide6.QtWidgets import QApplication, QScrollArea

from joyread.ui.widgets.book_detail import DetailThumbnailGrid
from joyread.ui.resources.styles.theme import Theme
from tests.unit.test_thumbnail_grid_cache import image_bytes


def test_caption_and_image_clicks_share_zero_based_page_but_gaps_do_not(qtbot):
    grid = DetailThumbnailGrid()
    qtbot.addWidget(grid)
    grid.resize(400, 500)
    grid.set_thumbnail_count(1000)
    grid.show()
    selected = []
    grid.thumbnail_clicked.connect(selected.append)
    for index in (0, 4):
        qtbot.mouseClick(grid, Qt.MouseButton.LeftButton, pos=grid._index_rect(index).center())
        qtbot.mouseClick(grid, Qt.MouseButton.LeftButton, pos=grid._item_rect(index).center())
        image = grid._item_rect(index)
        qtbot.mouseClick(grid, Qt.MouseButton.LeftButton, pos=QPoint(image.center().x(), image.bottom() + 2))
    assert selected == [0, 0, 4, 4]
    assert grid._index_at(grid._index_rect(999).center()) == 999
    assert grid._index_at(grid._index_rect(1000).center()) is None


def test_caption_height_is_used_by_visible_rows_and_reflow(qtbot):
    grid = DetailThumbnailGrid()
    qtbot.addWidget(grid)
    grid.resize(400, 500)
    grid.set_thumbnail_count(100)
    grid.show()
    qtbot.wait(1)
    columns = grid._columns
    row_y = grid._item_rect(columns).y()
    assert row_y - grid._item_rect(0).y() == grid._slot_height() + grid._vertical_spacing
    visible, _ = grid.visible_and_prefetch_indices(QRect(0, row_y, 400, 5))
    assert visible == tuple(range(columns, columns * 2))
    grid.set_interest((0,))
    grid.set_thumbnail(0, image_bytes())
    widget = grid._thumbnails[0]
    pixels = widget._pixmap.cacheKey()
    grid.resize(640, 500)
    qtbot.wait(1)
    assert grid._thumbnails[0] is widget and widget._pixmap.cacheKey() == pixels
    assert widget.height() == Theme.detail_thumbnail_height
    assert grid._index_height() >= QFontMetrics(grid._index_font()).height()


def test_caption_hit_cursor_and_unloaded_page_count_do_not_allocate_widgets(qtbot):
    scroll = QScrollArea()
    qtbot.addWidget(scroll)
    grid = DetailThumbnailGrid()
    scroll.setWidgetResizable(True)
    scroll.setWidget(grid)
    scroll.resize(400, 500)
    grid.set_thumbnail_count(100_000)
    scroll.show()
    qtbot.waitExposed(scroll)
    def move(point):
        QApplication.sendEvent(grid, QMouseEvent(QEvent.Type.MouseMove, point,
            grid.mapToGlobal(point), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
    move(grid._index_rect(0).center())
    assert grid.cursor().shape() == Qt.CursorShape.PointingHandCursor
    move(QPoint(1, 1))
    assert grid.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert not grid._thumbnails and not grid._retained
