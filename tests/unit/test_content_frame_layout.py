from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QLabel, QToolButton, QWidget

from joyread.core.models.book import Book
from joyread.core.repositories.mock_book_repository import MockBookRepository
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_card import BookCardWidget
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListRowWidget, BookListWidget
from joyread.ui.widgets.progress_bar import BookProgressBar
from joyread.ui.widgets.top_toolbar import TopToolbarWidget
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.views.shelf_view import ShelfView


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


def test_book_card_selected_outline_does_not_change_layout_geometry(qtbot) -> None:
    apply_theme()
    book = MockBookRepository().list_books()[0]
    card = BookCardWidget(book, ResourceLoader())
    qtbot.addWidget(card)

    margins = card.layout().contentsMargins()
    before = (card.size(), margins.left(), margins.top(), margins.right(), margins.bottom())

    card.set_selected(True)
    margins = card.layout().contentsMargins()
    after = (card.size(), margins.left(), margins.top(), margins.right(), margins.bottom())

    assert before == after
    assert margins.left() == Theme.book_card_layout_margin
    assert card.property("selected") == "true"


def test_book_progress_bar_clamps_value_and_keeps_figma_size(qtbot) -> None:
    progress = BookProgressBar(5)
    qtbot.addWidget(progress)
    indicator = progress.findChild(QFrame, "BookProgressIndicator")
    assert indicator is not None

    assert progress.size().width() == Theme.book_progress_width
    assert progress.size().height() == Theme.book_progress_height
    assert progress.progress_percent == 5
    assert indicator.width() == Theme.book_progress_height
    assert indicator.isHidden() is False

    progress.set_progress(-10)
    assert progress.progress_percent == 0
    assert indicator.isHidden() is True

    progress.set_progress(140)
    assert progress.progress_percent == 100
    assert indicator.width() == Theme.book_progress_width
    assert indicator.isHidden() is False


def test_book_list_row_matches_figma_structure_and_selected_variant(qtbot) -> None:
    apply_theme()
    book = MockBookRepository().list_books()[1]
    row = BookListRowWidget(book, ResourceLoader())
    qtbot.addWidget(row)

    cover = row.findChild(QWidget, "BookCover")
    progress_percent_labels = [
        label for label in row.findChildren(QLabel) if label.property("class") == "BookProgressPercent"
    ]

    assert cover is not None
    assert cover.size().width() == Theme.book_list_cover_width
    assert cover.size().height() == Theme.book_list_cover_height
    assert row.height() == Theme.book_list_row_height
    assert row.minimumWidth() == Theme.book_list_row_width
    assert progress_percent_labels
    assert progress_percent_labels[0].text() == f"{book.progress_percent}%"
    assert progress_percent_labels[0].isHidden() is False

    row.set_selected(True)
    assert progress_percent_labels[0].isHidden() is False


def test_detail_button_emits_only_its_own_book_in_multi_selection_context(qtbot) -> None:
    apply_theme()
    second = MockBookRepository().list_books()[1]
    row = BookListRowWidget(second, ResourceLoader())
    qtbot.addWidget(row)

    emitted: list[str] = []
    row.detail_requested.connect(emitted.append)
    row.set_selected(True)

    detail_button = row.findChildren(QToolButton)[0]
    qtbot.mouseClick(detail_button, Qt.MouseButton.LeftButton)

    assert emitted == [second.uuid]


def test_blank_grid_area_emits_clear_selection_signal(qtbot) -> None:
    apply_theme()
    grid = BookGridWidget(ResourceLoader())
    qtbot.addWidget(grid)
    grid.resize(400, 300)
    grid.show()

    with qtbot.waitSignal(grid.blank_clicked, timeout=1000):
        qtbot.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton)


def test_shelf_menu_targets_preserve_multi_selection(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(MockBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.render()

    first, second, third = viewmodel.visible_books[:3]
    viewmodel.select_book(first.uuid)
    viewmodel.select_book(second.uuid, additive=True)

    assert view._menu_target_ids(second.uuid) == (first.uuid, second.uuid)
    assert viewmodel.selected_book_ids == {first.uuid, second.uuid}

    assert view._menu_target_ids(third.uuid) == (third.uuid,)
    assert viewmodel.selected_book_ids == {third.uuid}


def test_stylesheet_resolves_content_and_scrollbar_tokens() -> None:
    stylesheet = ResourceLoader().load_stylesheet()

    assert "__WINDOW_RADIUS__" not in stylesheet
    assert "__CONTENT_COLOR__" not in stylesheet
    assert "__BOOK_CARD_RADIUS__" not in stylesheet
    assert "__CARD_BUTTON_RADIUS__" not in stylesheet
    assert "__CARD_SELECTED__" not in stylesheet
    assert "__BOOK_SELECTION_BORDER_WIDTH__" not in stylesheet
    assert "__PROGRESS_BACKGROUND__" not in stylesheet
    assert "__SHELF_SCROLLBAR_WIDTH__" not in stylesheet
    assert "__SHELF_SCROLLBAR_BOTTOM_MARGIN__" not in stylesheet
    assert "QScrollArea[class=\"ShelfScrollArea\"] QScrollBar:vertical" in stylesheet
    assert f"margin: 0 0 {Theme.shelf_scrollbar_bottom_margin}px 0;" in stylesheet
    assert f"border-bottom-right-radius: {Theme.window_corner_radius}px;" in stylesheet
    assert "background: #fafafa;" not in stylesheet
