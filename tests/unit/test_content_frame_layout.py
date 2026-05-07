from datetime import datetime
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QLabel, QToolButton, QWidget
from PIL import Image

from joyread.core.models.book import Book
from joyread.core.repositories.mock_book_repository import MockBookRepository
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.book_card import BookCardWidget, BookCoverWidget
from joyread.ui.widgets.book_detail import BookDetailPanel, DetailReadButton, DetailThumbnailGrid, DetailThumbnailWidget
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListRowWidget, BookListWidget
from joyread.ui.widgets.elided_label import ElidedLabel
from joyread.ui.widgets.progress_bar import BookProgressBar
from joyread.ui.widgets.top_toolbar import TopToolbarWidget
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.views.shelf_view import ShelfView


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def write_test_png(path: Path, size: tuple[int, int] = (40, 60), color: str = "#336699") -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


def make_test_image_bytes(size: tuple[int, int] = (40, 60), color: str = "#cc4422") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


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


def test_auto_hide_scroll_handle_reveals_then_hides_after_idle(qtbot) -> None:
    apply_theme()
    scroll_area = BookGridWidget(ResourceLoader())
    qtbot.addWidget(scroll_area)
    controller = AutoHideScrollHandle(scroll_area, hide_delay_ms=15)
    scrollbar = scroll_area.verticalScrollBar()

    assert scrollbar.property("scrollHandleVisible") == "false"
    assert controller.is_handle_visible is False

    scrollbar.setRange(0, 100)
    scrollbar.setValue(50)

    assert scrollbar.property("scrollHandleVisible") == "true"
    assert controller.is_handle_visible is True

    qtbot.wait(30)

    assert scrollbar.property("scrollHandleVisible") == "false"
    assert controller.is_handle_visible is False


def test_shelf_content_switches_left_outer_radius_when_sidebar_hidden(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(MockBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)

    assert view.property("sidebarVisible") == "true"

    view.set_sidebar_visible(False)

    assert view.property("sidebarVisible") == "false"

    view.set_sidebar_visible(True)

    assert view.property("sidebarVisible") == "true"


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


def test_grid_columns_change_only_after_card_width_plus_minimum_gap(qtbot) -> None:
    apply_theme()
    grid = BookGridWidget(ResourceLoader())
    qtbot.addWidget(grid)

    two_column_width = (2 * Theme.book_card_width) + Theme.grid_min_gap
    next_pixel_before = (3 * Theme.book_card_width) + (2 * Theme.grid_min_gap) - 1
    three_column_width = next_pixel_before + 1

    assert Theme.grid_min_gap == 20
    assert grid._calculate_columns_for_width(two_column_width) == 2
    assert grid._calculate_columns_for_width(next_pixel_before) == 2
    assert grid._calculate_columns_for_width(three_column_width) == 3


def test_grid_resize_keeps_card_widgets_stable(qtbot) -> None:
    apply_theme()
    grid = BookGridWidget(ResourceLoader())
    books = MockBookRepository().list_books()
    qtbot.addWidget(grid)
    grid.set_books(books, set())
    grid.show()
    QApplication.processEvents()

    card_ids = {book_uuid: id(card) for book_uuid, card in grid._cards.items()}
    for width in (900, 760, 680, 720, 860, 640, 934):
        grid.resize(width, 500)
        QApplication.processEvents()

    assert {book_uuid: id(card) for book_uuid, card in grid._cards.items()} == card_ids
    assert len(grid._cards) == len(books)


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


def test_elided_title_label_hides_overflow_and_tooltips_only_when_needed(qtbot) -> None:
    long_title = "This Is A Very Long JoyRead Book Title That Cannot Fit"
    label = ElidedLabel(long_title)
    qtbot.addWidget(label)
    label.resize(80, 20)
    label.show()
    QApplication.processEvents()

    assert label.text() != long_title
    assert label.toolTip() == long_title

    short_title = "Short"
    label.set_full_text(short_title)
    label.resize(200, 20)
    QApplication.processEvents()

    assert label.text() == short_title
    assert label.toolTip() == ""


def test_two_line_elided_label_reserves_two_lines_and_tooltips_when_clipped(qtbot) -> None:
    long_title = "This Is A Very Long JoyRead Book Title That Needs Two Display Lines"
    label = ElidedLabel(long_title, max_lines=2)
    qtbot.addWidget(label)
    label.resize(140, 100)
    label.show()
    QApplication.processEvents()

    assert label.max_lines == 2
    assert label.height() == (label.fontMetrics().lineSpacing() * 2) + Theme.elided_label_clip_guard
    assert len(label.text().splitlines()) <= 2
    assert label.text() != long_title
    assert label.toolTip() == long_title

    short_title = "Short Title"
    label.set_full_text(short_title)
    label.resize(600, 100)
    QApplication.processEvents()

    assert label.text() == short_title
    assert label.toolTip() == ""


def test_book_title_surfaces_reserve_two_lines(qtbot) -> None:
    apply_theme()
    now = datetime(2026, 1, 1)
    book = Book(
        uuid="long-title",
        title="A Very Long JoyRead Title That Should Use The Two Line Display Space",
        author="Author",
        language_tag="en",
        book_type="Comic",
        file_format="CBZ",
        file_path="/tmp/book.cbz",
        progress=0.25,
        cover_thumbnail_path=None,
        added_at=now,
        updated_at=now,
        last_read_at=None,
        is_favourite=False,
    )
    card = BookCardWidget(book, ResourceLoader())
    row = BookListRowWidget(book, ResourceLoader())
    detail = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(card)
    qtbot.addWidget(row)
    qtbot.addWidget(detail)
    detail.set_book(book)
    for widget in (card, row, detail):
        widget.show()
    QApplication.processEvents()

    title_labels = [
        label
        for widget in (card, row, detail)
        for label in widget.findChildren(ElidedLabel)
        if label.property("class") in {"BookTitle", "BookDetailTitle"}
    ]

    assert len(title_labels) == 3
    assert all(label.max_lines == 2 for label in title_labels)
    assert all(
        label.height() == (label.fontMetrics().lineSpacing() * 2) + Theme.elided_label_clip_guard
        for label in title_labels
    )
    assert card.height() == Theme.book_card_height


def test_book_card_cover_can_update_from_generated_path(qtbot, tmp_path) -> None:
    apply_theme()
    cover_path = tmp_path / "cover.png"
    write_test_png(cover_path)
    book = MockBookRepository().list_books()[0]
    card = BookCardWidget(book, ResourceLoader())
    qtbot.addWidget(card)
    cover = card.findChild(BookCoverWidget, "BookCover")
    assert cover is not None
    before = cover._pixmap.cacheKey()

    card.set_cover_path(cover_path)

    assert cover._pixmap.cacheKey() != before


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
    author_labels = [label for label in row.findChildren(QLabel) if label.property("class") == "BookAuthor"]

    assert cover is not None
    assert cover.size().width() == Theme.book_list_cover_width
    assert cover.size().height() == Theme.book_list_cover_height
    assert row.height() == Theme.book_list_row_height
    assert row.minimumWidth() == Theme.book_list_row_width
    assert author_labels
    assert author_labels[0].text() == book.author
    assert progress_percent_labels
    assert progress_percent_labels[0].text() == f"{book.progress_percent}%"
    assert progress_percent_labels[0].isHidden() is False

    row.set_selected(True)
    assert progress_percent_labels[0].isHidden() is False


def test_book_list_row_cover_can_update_from_generated_path(qtbot, tmp_path) -> None:
    apply_theme()
    cover_path = tmp_path / "list-cover.png"
    write_test_png(cover_path)
    row = BookListRowWidget(MockBookRepository().list_books()[0], ResourceLoader())
    qtbot.addWidget(row)
    cover = row.findChild(BookCoverWidget, "BookCover")
    assert cover is not None
    before = cover._pixmap.cacheKey()

    row.set_cover_path(cover_path)

    assert cover._pixmap.cacheKey() != before


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


def test_book_detail_panel_binds_figma_metadata_and_starts_without_page_count_thumbnails(qtbot) -> None:
    apply_theme()
    book = MockBookRepository().list_books()[1]
    panel = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(panel)
    panel.resize(876, 760)
    panel.set_book(book)
    panel.show()
    QApplication.processEvents()

    title_labels = [label for label in panel.findChildren(QLabel) if label.property("class") == "BookDetailTitle"]
    author_labels = [label for label in panel.findChildren(QLabel) if label.property("class") == "BookDetailAuthor"]
    pill_labels = [label.text() for label in panel.findChildren(QLabel) if label.property("class") == "BookDetailPillText"]
    progress = panel.findChild(BookProgressBar)
    progress_unit = panel.findChild(QWidget, "BookDetailProgressUnit")
    cover_panel = panel.findChild(QWidget, "BookDetailCoverPanel")
    cover = panel.findChild(QWidget, "BookCover")
    read_button = panel.findChild(DetailReadButton)
    thumbnails = panel.findChildren(DetailThumbnailWidget)

    assert title_labels[0].text() == book.title
    assert author_labels[0].text() == f"Author: {book.author}"
    assert f"Language: {book.language_tag}" in pill_labels
    assert f"Book Type: {book.file_format}" in pill_labels
    assert cover_panel is not None
    cover_margins = cover_panel.layout().contentsMargins()
    assert (cover_margins.left(), cover_margins.top(), cover_margins.right(), cover_margins.bottom()) == (
        0,
        0,
        0,
        Theme.detail_cover_panel_bottom_padding,
    )
    assert cover_panel.layout().spacing() == Theme.detail_cover_panel_gap
    assert progress_unit is not None
    assert progress_unit.width() == Theme.detail_progress_unit_width
    assert progress_unit.layout().spacing() == 0
    assert progress_unit.layout().itemAt(1).spacerItem() is not None
    assert cover is not None
    assert progress_unit.geometry().center().x() == cover.geometry().center().x()
    assert progress is not None
    assert progress.width() == Theme.detail_progress_width
    assert read_button is not None
    assert read_button.size() == QSize(Theme.detail_read_button_width, Theme.detail_button_size)
    read_margins = read_button.layout().contentsMargins()
    assert (read_margins.left(), read_margins.top(), read_margins.right(), read_margins.bottom()) == (
        Theme.detail_button_layout_margin,
        Theme.detail_button_layout_margin,
        Theme.detail_button_layout_margin,
        Theme.detail_button_layout_margin,
    )
    # Detail thumbnails are now archive-discovered in async batches. The panel
    # must not allocate widgets from the mock/database page_count during open.
    assert len(thumbnails) == 0

    emitted: list[str] = []
    panel.read_requested.connect(emitted.append)
    qtbot.mouseClick(read_button, Qt.MouseButton.LeftButton)
    assert emitted == [book.uuid]


def test_book_detail_panel_cover_can_update_for_current_book(qtbot, tmp_path) -> None:
    apply_theme()
    cover_path = tmp_path / "detail-cover.png"
    write_test_png(cover_path, size=(200, 284))
    book = MockBookRepository().list_books()[0]
    panel = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(panel)
    panel.set_book(book)
    cover = panel.findChild(BookCoverWidget, "BookCover")
    assert cover is not None
    before = cover._pixmap.cacheKey()

    panel.set_cover_path(book.uuid, cover_path)

    assert cover._pixmap.cacheKey() != before


def test_detail_thumbnail_grid_uses_figma_ideal_seven_column_spacing(qtbot) -> None:
    grid = DetailThumbnailGrid()
    qtbot.addWidget(grid)
    grid.resize(864, 400)
    grid.set_thumbnail_count(14)
    grid.show()
    QApplication.processEvents()

    assert grid._calculate_columns() == 7
    assert grid._calculate_horizontal_spacing(7) == Theme.detail_thumbnail_gap


def test_detail_thumbnail_grid_updates_single_thumbnail_from_bytes(qtbot) -> None:
    grid = DetailThumbnailGrid()
    qtbot.addWidget(grid)
    grid.set_thumbnail_count(2)

    grid.set_thumbnail(1, make_test_image_bytes())

    assert grid._thumbnails[0]._pixmap is None
    assert grid._thumbnails[1]._pixmap is not None


def test_book_detail_panel_requests_more_thumbnails_only_after_visible(qtbot) -> None:
    apply_theme()
    book = MockBookRepository().list_books()[0]
    panel = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(panel)
    panel.set_book(book)

    emitted: list[str] = []
    panel.more_thumbnails_requested.connect(emitted.append)
    panel._emit_more_thumbnails_if_near_bottom()
    assert emitted == []

    panel.show()
    QApplication.processEvents()
    panel._emit_more_thumbnails_if_near_bottom()

    assert emitted == [book.uuid]


def test_shelf_detail_panel_uses_parent_relative_figma_geometry(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(MockBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.resize(934, 841)
    view.show()
    QApplication.processEvents()

    book = viewmodel.visible_books[0]
    viewmodel.show_detail(book.uuid)
    QApplication.processEvents()

    assert view.detail_panel.isVisible()
    assert view.detail_panel.geometry().getRect() == (
        Theme.detail_panel_horizontal_margin,
        Theme.detail_panel_top_margin,
        876,
        760,
    )

    view.resize(1000, 700)
    QApplication.processEvents()

    assert view.detail_panel.geometry().getRect() == (
        Theme.detail_panel_horizontal_margin,
        Theme.detail_panel_top_margin,
        1000 - (Theme.detail_panel_horizontal_margin * 2),
        700 - Theme.detail_panel_top_margin,
    )

    view.resize(620, 500)
    QApplication.processEvents()

    assert view.detail_panel.geometry().getRect() == (
        Theme.detail_panel_horizontal_margin,
        Theme.detail_panel_top_margin,
        620 - (Theme.detail_panel_horizontal_margin * 2),
        500 - Theme.detail_panel_top_margin,
    )


def test_shelf_detail_panel_closes_with_escape(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(MockBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.resize(934, 841)
    view.show()
    view.setFocus()
    QApplication.processEvents()

    book = viewmodel.visible_books[0]
    viewmodel.show_detail(book.uuid)
    QApplication.processEvents()

    assert view.detail_panel.isVisible()

    qtbot.keyClick(view, Qt.Key.Key_Escape)
    QApplication.processEvents()

    assert viewmodel.detail_book_uuid is None
    assert view.detail_panel.isHidden()


def test_shelf_detail_panel_closes_on_blank_shelf_click(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(MockBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.resize(934, 841)
    view.show()
    QApplication.processEvents()

    first, second = viewmodel.visible_books[:2]
    viewmodel.select_book(first.uuid)
    viewmodel.select_book(second.uuid, additive=True)
    viewmodel.show_detail(first.uuid)
    QApplication.processEvents()

    assert view.detail_panel.isVisible()
    assert viewmodel.selected_book_ids == {first.uuid, second.uuid}

    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    QApplication.processEvents()

    assert viewmodel.detail_book_uuid is None
    assert view.detail_panel.isHidden()
    assert viewmodel.selected_book_ids == set()


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
    assert "__DETAIL_PANEL_BACKGROUND__" not in stylesheet
    assert "__DETAIL_PANEL_RADIUS__" not in stylesheet
    assert "__SHELF_SCROLLBAR_WIDTH__" not in stylesheet
    assert "__SHELF_SCROLLBAR_BOTTOM_MARGIN__" not in stylesheet
    assert "__SHELF_SCROLLBAR_HANDLE_HIDDEN__" not in stylesheet
    assert 'QWidget#ShelfContent[sidebarVisible="false"]' in stylesheet
    assert f"border-bottom-left-radius: {Theme.window_corner_radius}px;" in stylesheet
    assert "QScrollArea[class=\"ShelfScrollArea\"] QScrollBar:vertical" in stylesheet
    assert 'QScrollBar[scrollHandleVisible="false"]::handle:vertical' in stylesheet
    assert f"margin: 0 0 {Theme.shelf_scrollbar_bottom_margin}px 0;" in stylesheet
    assert f"border-bottom-right-radius: {Theme.window_corner_radius}px;" in stylesheet
    assert "background: #fafafa;" not in stylesheet
