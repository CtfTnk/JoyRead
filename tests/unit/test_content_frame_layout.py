from dataclasses import replace
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QLabel, QLineEdit, QScrollArea, QToolButton, QWidget
from PIL import Image

from joyread.core.models.book import Book
from joyread.core.models.tag import Tag
from tests.support.in_memory_book_repository import InMemoryBookRepository
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.book_card import BookCardWidget, BookCoverWidget
from joyread.ui.widgets.book_detail import (
    BookDetailPanel,
    DetailReadButton,
    DetailThumbnailGrid,
    DetailThumbnailWidget,
    InlineEditableText,
)
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListRowWidget, BookListWidget
from joyread.ui.widgets.elided_label import ElidedLabel
from joyread.ui.widgets.progress_bar import BookProgressBar
from joyread.ui.widgets.tag_chip import TagChipWidget
from joyread.ui.widgets.top_toolbar import TopToolbarWidget
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey, ShelfViewModel, collection_shelf_key
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
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
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
    books = InMemoryBookRepository().list_books()
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


def test_grid_reused_card_updates_metadata_in_real_time(qtbot) -> None:
    apply_theme()
    grid = BookGridWidget(ResourceLoader())
    books = InMemoryBookRepository().list_books()
    qtbot.addWidget(grid)
    grid.set_books(books, set())
    grid.show()
    QApplication.processEvents()

    target = books[0]
    card = grid._cards[target.uuid]
    card_id = id(card)
    updated_books = [
        replace(target, title="Updated Card Title", progress=0.99)
        if book.uuid == target.uuid
        else book
        for book in books
    ]

    grid.set_books(updated_books, set())
    QApplication.processEvents()

    assert id(grid._cards[target.uuid]) == card_id
    assert card.book.title == "Updated Card Title"
    title_label = next(label for label in card.findChildren(ElidedLabel) if label.property("class") == "BookTitle")
    progress = card.findChild(BookProgressBar)
    assert title_label.full_text == "Updated Card Title"
    assert progress is not None
    assert progress.progress_percent == 99


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
    book = InMemoryBookRepository().list_books()[0]
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
    assert label.text().count("\n") == 1
    assert len(label.text().splitlines()) == 2
    assert label.text() != long_title
    assert label.toolTip() == long_title

    short_title = "Short Title"
    label.set_full_text(short_title)
    label.resize(600, 100)
    QApplication.processEvents()

    assert label.text() == short_title
    assert label.toolTip() == ""
    # Short content shrinks the label to a single-line height even though
    # max_lines=2 — that's how the list row / detail panel push the author
    # label up directly under short titles.
    assert label.height() == label.fontMetrics().lineSpacing() + Theme.elided_label_clip_guard


def test_two_line_elided_label_reserves_full_height_when_requested(qtbot) -> None:
    label = ElidedLabel("Short", max_lines=2, reserve_full_height=True)
    qtbot.addWidget(label)
    label.resize(400, 100)
    label.show()
    QApplication.processEvents()

    assert label.text() == "Short"
    assert label.height() == (label.fontMetrics().lineSpacing() * 2) + Theme.elided_label_clip_guard


def test_book_title_surfaces_height_matches_actual_lines(qtbot) -> None:
    apply_theme()
    now = datetime(2026, 1, 1)
    long_book = Book(
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
    short_book = replace(long_book, uuid="short-title", title="Short")

    card = BookCardWidget(long_book, ResourceLoader())
    row = BookListRowWidget(long_book, ResourceLoader())
    detail = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(card)
    qtbot.addWidget(row)
    qtbot.addWidget(detail)
    detail.set_book(long_book)
    for widget in (card, row, detail):
        widget.show()
    QApplication.processEvents()

    def _title_label(widget) -> ElidedLabel:
        return next(
            label
            for label in widget.findChildren(ElidedLabel)
            if label.property("class") in {"BookTitle", "BookDetailTitle"}
        )

    card_title = _title_label(card)
    row_title = _title_label(row)
    detail_title = _title_label(detail)
    guard = Theme.elided_label_clip_guard

    def expected(label: ElidedLabel, lines: int) -> int:
        return (label.fontMetrics().lineSpacing() * lines) + guard

    # Grid card always reserves two-line height so its control bar lines up
    # across cards regardless of title length.
    assert card_title.height() == expected(card_title, 2)

    # List row and detail panel shrink to fit when the title is short and
    # expand to two lines when the title wraps.
    assert row_title.height() == expected(row_title, 2)
    assert detail_title.height() == expected(detail_title, 2)

    card.set_book(short_book)
    # BookListRowWidget builds its title in __init__; instantiate a fresh
    # row for the short book instead of mutating the existing one.
    short_row = BookListRowWidget(short_book, ResourceLoader())
    qtbot.addWidget(short_row)
    short_row.show()
    detail.set_book(short_book)
    QApplication.processEvents()

    short_card_title = _title_label(card)
    short_row_title = _title_label(short_row)
    short_detail_title = _title_label(detail)
    assert short_card_title.height() == expected(short_card_title, 2)
    assert short_row_title.height() == expected(short_row_title, 1)
    assert short_detail_title.height() == expected(short_detail_title, 1)
    assert card.height() == Theme.book_card_height


def test_book_card_cover_can_update_from_generated_path(qtbot, tmp_path) -> None:
    apply_theme()
    cover_path = tmp_path / "cover.png"
    write_test_png(cover_path)
    book = InMemoryBookRepository().list_books()[0]
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
    book = InMemoryBookRepository().list_books()[1]
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
    row = BookListRowWidget(InMemoryBookRepository().list_books()[0], ResourceLoader())
    qtbot.addWidget(row)
    cover = row.findChild(BookCoverWidget, "BookCover")
    assert cover is not None
    before = cover._pixmap.cacheKey()

    row.set_cover_path(cover_path)

    assert cover._pixmap.cacheKey() != before


def test_detail_button_emits_only_its_own_book_in_multi_selection_context(qtbot) -> None:
    apply_theme()
    second = InMemoryBookRepository().list_books()[1]
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
    book = InMemoryBookRepository().list_books()[1]
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
    tag_box = panel.findChild(QFrame, "BookDetailTagBox")
    tag_scroll = panel.findChild(QScrollArea, "BookDetailTagScrollArea")
    thumbnails = panel.findChildren(DetailThumbnailWidget)

    assert title_labels[0].text() == book.title
    assert author_labels[0].text() == f"Author: {book.author}"
    assert f"Language: {book.language_name}" in pill_labels
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
    assert tag_box is not None
    assert tag_box.height() == Theme.detail_tag_box_height
    assert tag_scroll is not None
    assert tag_box.findChildren(AutoHideScrollHandle)
    assert read_button is not None
    assert read_button.size() == QSize(Theme.detail_read_button_width, Theme.detail_button_size)
    read_shadow = read_button.graphicsEffect()
    assert read_shadow is not None
    assert read_shadow.offset().x() == 0
    assert read_shadow.offset().y() == 1
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


def test_book_detail_tag_box_shrinks_when_title_uses_two_rows(qtbot) -> None:
    apply_theme()
    book = replace(
        InMemoryBookRepository().list_books()[1],
        title="A Very Long Manga Title That Must Wrap Onto Two Rows In The Detail Panel",
    )
    panel = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(panel)
    panel.resize(520, 760)
    panel.set_book(book)
    panel.show()
    QApplication.processEvents()
    QApplication.processEvents()

    tag_box = panel.findChild(QFrame, "BookDetailTagBox")

    assert tag_box is not None
    assert tag_box.height() == Theme.detail_tag_box_compact_height


def test_book_detail_tag_chips_emit_filter_and_allocation_requests(qtbot) -> None:
    apply_theme()
    book = InMemoryBookRepository().list_books()[1]
    panel = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(panel)
    panel.resize(876, 760)
    panel.set_book(
        book,
        tags=(Tag("tag-action", "Action"), Tag("tag-comedy", "Comedy")),
    )
    panel.show()
    QApplication.processEvents()

    filter_requests: list[tuple[str, str]] = []
    allocation_requests: list[str] = []
    panel.tag_filter_requested.connect(lambda book_uuid, tag_id: filter_requests.append((book_uuid, tag_id)))
    panel.tag_allocation_requested.connect(allocation_requests.append)
    chips = panel.findChildren(TagChipWidget)
    tag_chip = next(chip for chip in chips if chip.tag_id == "tag-action")
    add_chip = next(chip for chip in chips if chip.is_add_chip)

    qtbot.mouseClick(tag_chip, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(add_chip, Qt.MouseButton.LeftButton)

    assert filter_requests == [(book.uuid, "tag-action")]
    assert allocation_requests == [book.uuid]


def test_book_detail_inline_edits_emit_metadata_change_requests(qtbot) -> None:
    apply_theme()
    book = InMemoryBookRepository().list_books()[1]
    panel = BookDetailPanel(ResourceLoader())
    emitted_titles: list[tuple[str, str]] = []
    emitted_authors: list[tuple[str, str]] = []
    panel.title_change_requested.connect(lambda book_uuid, value: emitted_titles.append((book_uuid, value)))
    panel.author_change_requested.connect(lambda book_uuid, value: emitted_authors.append((book_uuid, value)))
    qtbot.addWidget(panel)
    panel.set_book(book)
    panel.show()
    QApplication.processEvents()

    fields = panel.findChildren(InlineEditableText)
    title_field, author_field = fields[0], fields[1]

    title_field._begin_edit()
    title_editor = title_field.findChild(QLineEdit)
    assert title_editor is not None
    title_editor.setText("Edited Detail Title")
    qtbot.keyClick(title_editor, Qt.Key.Key_Return)

    author_field._begin_edit()
    author_editor = author_field.findChild(QLineEdit)
    assert author_editor is not None
    author_editor.setText("Edited Author")
    qtbot.keyClick(author_editor, Qt.Key.Key_Return)

    assert emitted_titles == [(book.uuid, "Edited Detail Title")]
    assert emitted_authors == [(book.uuid, "Edited Author")]


def test_book_detail_language_pill_emits_menu_request_on_double_click(qtbot) -> None:
    apply_theme()
    book = InMemoryBookRepository().list_books()[1]
    panel = BookDetailPanel(ResourceLoader())
    emitted: list[tuple[str, QPoint]] = []
    panel.language_menu_requested.connect(lambda book_uuid, point: emitted.append((book_uuid, point)))
    qtbot.addWidget(panel)
    panel.set_book(book)
    panel.show()
    QApplication.processEvents()

    language_label = next(
        label
        for label in panel.findChildren(QLabel)
        if label.property("class") == "BookDetailPillText" and label.text().startswith("Language:")
    )
    qtbot.mouseDClick(language_label, Qt.MouseButton.LeftButton)

    assert emitted
    assert emitted[0][0] == book.uuid


def test_book_detail_language_pill_keeps_full_text_across_repeated_changes(qtbot) -> None:
    apply_theme()
    book = InMemoryBookRepository().list_books()[1]
    panel = BookDetailPanel(ResourceLoader())
    qtbot.addWidget(panel)
    panel.show()

    for language_tag, language_name in (
        ("zh", "Chinese"),
        ("ja", "Japanese"),
        ("und", "Unknown"),
        ("en", "English"),
    ):
        panel.set_book(replace(book, language_tag=language_tag, language_name=language_name))
        QApplication.processEvents()
        language_label = next(
            label
            for label in panel.findChildren(QLabel)
            if label.property("class") == "BookDetailPillText" and label.text().startswith("Language:")
        )
        expected = f"Language: {language_name}"
        assert language_label.text() == expected
        assert "..." not in language_label.text()
        assert "…" not in language_label.text()
        assert language_label.sizeHint().width() >= language_label.fontMetrics().horizontalAdvance(expected)


def test_book_detail_panel_cover_can_update_for_current_book(qtbot, tmp_path) -> None:
    apply_theme()
    cover_path = tmp_path / "detail-cover.png"
    write_test_png(cover_path, size=(200, 284))
    book = InMemoryBookRepository().list_books()[0]
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
    book = InMemoryBookRepository().list_books()[0]
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


def test_shelf_view_defers_thumbnail_updates_while_popup_is_active(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.show()
    book = viewmodel.visible_books[0]
    viewmodel.show_detail(book.uuid)
    QApplication.processEvents()

    view._popup_interaction_depth = 1
    view._handle_page_thumbnail_ready(book.uuid, 0, make_test_image_bytes())

    assert view.detail_panel._thumbnail_grid._thumbnails == {}

    view._popup_interaction_depth = 0
    assert view._flush_deferred_detail_thumbnail_updates() is False

    assert 0 in view.detail_panel._thumbnail_grid._thumbnails


def test_shelf_view_drops_deferred_thumbnail_updates_after_detail_book_changes(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.show()
    first, second = viewmodel.visible_books[:2]
    viewmodel.show_detail(first.uuid)
    QApplication.processEvents()

    view._popup_interaction_depth = 1
    view._handle_page_thumbnail_ready(first.uuid, 0, make_test_image_bytes())
    viewmodel.show_detail(second.uuid)
    QApplication.processEvents()

    view._popup_interaction_depth = 0
    assert view._flush_deferred_detail_thumbnail_updates() is False

    assert view.detail_panel._thumbnail_grid._thumbnails == {}


def test_shelf_view_defers_next_thumbnail_batch_until_popup_closes(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.show()
    book = viewmodel.visible_books[0]
    requested: list[str] = []
    view._request_next_detail_thumbnail_batch = requested.append  # type: ignore[method-assign]
    viewmodel.show_detail(book.uuid)
    QApplication.processEvents()
    requested.clear()

    view._popup_interaction_depth = 1
    view._handle_detail_thumbnail_batch_finished(book.uuid, 14, True)

    assert requested == []

    view._popup_interaction_depth = 0
    assert view._flush_deferred_detail_thumbnail_updates() is True
    QApplication.processEvents()

    assert requested == [book.uuid]


def test_shelf_detail_panel_uses_parent_relative_figma_geometry(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
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
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
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
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
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
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
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


def test_shelf_export_menu_request_uses_selected_targets(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    view.render()
    captured_menus: list[QWidget] = []
    emitted: list[tuple[str, ...]] = []
    view._exec_interaction_popup = lambda menu, _pos: captured_menus.append(menu)  # type: ignore[method-assign]
    view.export_books_requested.connect(emitted.append)

    first, second, third = viewmodel.visible_books[:3]
    viewmodel.select_book(first.uuid)
    viewmodel.select_book(second.uuid, additive=True)

    view._show_book_menu(second.uuid, QPoint(0, 0))
    _trigger_menu_row(qtbot, captured_menus.pop(), "Export")

    assert emitted == [(first.uuid, second.uuid)]

    view._show_book_menu(third.uuid, QPoint(0, 0))
    _trigger_menu_row(qtbot, captured_menus.pop(), "Export")

    assert emitted == [(first.uuid, second.uuid), (third.uuid,)]


def test_shelf_empty_state_copy_is_contextual(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository(books=[])))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)

    view.render()

    assert _state_label_texts(view.empty_state) == (
        "No books yet",
        "Use Open & Import or Import to add books to your bookshelf.",
    )

    viewmodel.set_current_shelf(collection_shelf_key("collection-a"))
    view.render()

    assert _state_label_texts(view.empty_state) == (
        "No books in this collection",
        "Add books to this collection from a book's More menu.",
    )


def test_shelf_remove_menu_visibility_is_contextual(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
    viewmodel.load_books()
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    captured_menus: list[QWidget] = []
    view._exec_interaction_popup = lambda menu, _pos: captured_menus.append(menu)  # type: ignore[method-assign]

    view._show_book_menu(viewmodel.visible_books[0].uuid, QPoint(0, 0))
    assert "Remove" not in _menu_row_labels(captured_menus.pop())

    viewmodel.set_current_shelf(ShelfKey.FAVOURITES.value)
    view._show_book_menu(viewmodel.visible_books[0].uuid, QPoint(0, 0))
    assert "Remove" not in _menu_row_labels(captured_menus.pop())

    viewmodel.set_current_shelf(ShelfKey.RECENT.value)
    view._show_book_menu(viewmodel.visible_books[0].uuid, QPoint(0, 0))
    assert "Remove" in _menu_row_labels(captured_menus.pop())

    viewmodel.set_current_shelf(collection_shelf_key("collection-a"))
    view._show_book_menu(viewmodel.visible_books[0].uuid, QPoint(0, 0))
    assert "Remove" in _menu_row_labels(captured_menus.pop())


def test_shelf_remove_menu_removes_from_collection_without_deleting_books(qtbot) -> None:
    apply_theme()
    viewmodel = ShelfViewModel(LibraryService(InMemoryBookRepository()))
    viewmodel.load_books()
    viewmodel.set_current_shelf(collection_shelf_key("collection-a"))
    view = ShelfView(viewmodel, ResourceLoader())
    qtbot.addWidget(view)
    captured_menus: list[QWidget] = []
    view._exec_interaction_popup = lambda menu, _pos: captured_menus.append(menu)  # type: ignore[method-assign]
    first, second = viewmodel.visible_books[:2]
    viewmodel.select_book(first.uuid)
    viewmodel.select_book(second.uuid, additive=True)

    view._show_book_menu(second.uuid, QPoint(0, 0))
    _trigger_menu_row(qtbot, captured_menus.pop(), "Remove")

    books_by_uuid = {book.uuid: book for book in viewmodel.books}
    assert first.uuid in books_by_uuid
    assert second.uuid in books_by_uuid
    assert "collection-a" not in books_by_uuid[first.uuid].collection_ids
    assert "collection-a" not in books_by_uuid[second.uuid].collection_ids


def _state_label_texts(widget: QWidget) -> tuple[str, str]:
    title = next(label.text() for label in widget.findChildren(QLabel) if label.property("class") == "StateTitle")
    body = next(label.text() for label in widget.findChildren(QLabel) if label.property("class") == "StateBody")
    return title, body


def _menu_row_labels(menu: QWidget) -> list[str]:
    return [row.findChild(QLabel).text() for row in menu.findChildren(QFrame) if row.objectName() == "FigmaMenuItem"]


def _trigger_menu_row(qtbot, menu: QWidget, label_text: str) -> None:  # noqa: ANN001
    qtbot.addWidget(menu)
    menu.show()
    QApplication.processEvents()
    rows = [widget for widget in menu.findChildren(QFrame) if widget.objectName() == "FigmaMenuItem"]
    row = next(row for row in rows if row.findChild(QLabel).text() == label_text)
    qtbot.mousePress(row, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(row, Qt.MouseButton.LeftButton)
    QApplication.processEvents()


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
    assert "__DETAIL_TAG_BOX_BACKGROUND__" not in stylesheet
    assert "__SHELF_SCROLLBAR_WIDTH__" not in stylesheet
    assert "__SHELF_SCROLLBAR_BOTTOM_MARGIN__" not in stylesheet
    assert "__SHELF_SCROLLBAR_HANDLE_HIDDEN__" not in stylesheet
    assert (
        f"background: {Theme._hex_rgba_qss(Theme.color_window, Theme.detail_tag_box_background_opacity)};"
        in stylesheet
    )
    assert 'QWidget#ShelfContent[sidebarVisible="false"]' in stylesheet
    assert f"border-bottom-left-radius: {Theme.window_corner_radius}px;" in stylesheet
    assert "QScrollArea[class=\"ShelfScrollArea\"] QScrollBar:vertical" in stylesheet
    assert 'QScrollBar[scrollHandleVisible="false"]::handle:vertical' in stylesheet
    assert f"margin: 0 0 {Theme.shelf_scrollbar_bottom_margin}px 0;" in stylesheet
    assert f"border-bottom-right-radius: {Theme.window_corner_radius}px;" in stylesheet
    assert "background: #fafafa;" not in stylesheet
