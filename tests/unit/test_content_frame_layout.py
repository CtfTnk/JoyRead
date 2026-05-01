from datetime import datetime

from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_card import BookCardWidget
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListWidget
from joyread.ui.widgets.top_toolbar import TopToolbarWidget


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def test_toolbar_uses_figma_content_frame_padding(qtbot) -> None:
    apply_theme()
    toolbar = TopToolbarWidget(ResourceLoader())
    qtbot.addWidget(toolbar)

    margins = toolbar.layout().contentsMargins()
    expected = Theme.content_horizontal_padding + Theme.banner_horizontal_padding

    assert toolbar.height() == Theme.toolbar_height
    assert (margins.left(), margins.right()) == (expected, expected)


def test_shelf_scroll_areas_keep_scrollbar_at_outer_edge(qtbot) -> None:
    apply_theme()

    for widget in (BookGridWidget(ResourceLoader()), BookListWidget(ResourceLoader())):
        qtbot.addWidget(widget)
        assert widget.property("class") == "ShelfScrollArea"
        assert widget.viewport().objectName() == "ShelfScrollViewport"

        content = widget.findChild(QWidget, "BookGridContent") or widget.findChild(QWidget, "BookListContent")
        assert content is not None
        margins = content.layout().contentsMargins()
        assert margins.left() == Theme.content_horizontal_padding
        assert margins.right() == Theme.content_scrollbar_adjusted_right_padding
        assert margins.top() == Theme.grid_top_padding
        assert margins.bottom() == Theme.grid_bottom_padding


def test_grid_spacing_justifies_cards_across_available_row_width(qtbot) -> None:
    apply_theme()
    grid = BookGridWidget(ResourceLoader())
    qtbot.addWidget(grid)
    grid.resize(Theme.content_frame_width, 500)
    grid.show()
    QApplication.processEvents()

    columns = grid._calculate_columns()
    if columns > 1:
        available_width = grid._available_row_width()
        expected_gap = (available_width - (columns * Theme.book_card_width)) // (columns - 1)
        assert grid._calculate_horizontal_spacing(columns) == max(Theme.grid_gap, expected_gap)


def test_grid_resize_buffer_skips_small_spacing_only_changes(qtbot) -> None:
    apply_theme()
    grid = BookGridWidget(ResourceLoader())
    qtbot.addWidget(grid)
    grid.resize(Theme.content_frame_width, 500)
    grid.show()
    QApplication.processEvents()
    grid._relayout(force=True)

    grid.viewport().resize(grid.viewport().width() + Theme.grid_resize_relayout_buffer - 1, grid.viewport().height())

    if grid._calculate_columns_for_width(grid._available_row_width()) == grid._columns:
        assert grid._should_relayout_after_resize() is False


def test_missing_book_card_uses_figma_opacity_without_color_tint(qtbot) -> None:
    apply_theme()
    now = datetime(2026, 1, 1)
    book = Book(
        uuid="missing",
        title="Missing",
        author=None,
        language_tag="en",
        book_type="Comic",
        file_format="CBZ",
        file_path="/missing.cbz",
        progress=0.0,
        cover_thumbnail_path=None,
        added_at=now,
        updated_at=now,
        last_read_at=None,
        is_favourite=False,
        is_missing=True,
    )
    card = BookCardWidget(book, ResourceLoader())
    qtbot.addWidget(card)

    effect = card.graphicsEffect()
    assert isinstance(effect, QGraphicsOpacityEffect)
    assert effect.opacity() == Theme.missing_book_opacity


def test_stylesheet_resolves_content_and_scrollbar_tokens() -> None:
    stylesheet = ResourceLoader().load_stylesheet()

    assert "__WINDOW_RADIUS__" not in stylesheet
    assert "__CONTENT_COLOR__" not in stylesheet
    assert "__SHELF_SCROLLBAR_WIDTH__" not in stylesheet
    assert "__SHELF_SCROLLBAR_BOTTOM_MARGIN__" not in stylesheet
    assert "QScrollArea[class=\"ShelfScrollArea\"] QScrollBar:vertical" in stylesheet
    assert f"margin: 0 0 {Theme.shelf_scrollbar_bottom_margin}px 0;" in stylesheet
    assert f"border-bottom-right-radius: {Theme.window_corner_radius}px;" in stylesheet
    assert "background: #fafafa;" not in stylesheet
