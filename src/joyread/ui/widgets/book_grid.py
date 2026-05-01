"""Responsive grid view for book cards."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal as QtSignal
from PySide6.QtWidgets import QGridLayout, QScrollArea, QSizePolicy, QSpacerItem, QWidget

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_card import BookCardWidget


class BookGridWidget(QScrollArea):
    book_selected = QtSignal(str, bool)
    book_opened = QtSignal(str)
    detail_requested = QtSignal(str)
    menu_requested = QtSignal(str, QPoint)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self.setProperty("class", "ShelfScrollArea")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setObjectName("ShelfScrollViewport")
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._books: list[Book] = []
        self._selected_ids: set[str] = set()
        self._cards: dict[str, BookCardWidget] = {}
        self._book_ids: tuple[str, ...] = ()
        self._columns = 0
        self._last_layout_width = 0
        self._last_horizontal_spacing = Theme.grid_gap

        self._content = QWidget()
        self._content.setObjectName("BookGridContent")
        self._layout = QGridLayout(self._content)
        self._layout.setContentsMargins(
            Theme.content_horizontal_padding,
            Theme.grid_top_padding,
            Theme.content_scrollbar_adjusted_right_padding,
            Theme.grid_bottom_padding,
        )
        self._layout.setHorizontalSpacing(Theme.grid_gap)
        self._layout.setVerticalSpacing(20)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setWidget(self._content)

    def set_books(self, books: list[Book], selected_ids: set[str]) -> None:
        self._books = list(books)
        self._selected_ids = set(selected_ids)
        self._relayout(force=True)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._should_relayout_after_resize():
            self._relayout()

    def _relayout(self, force: bool = False) -> None:
        available_width = self._available_row_width()
        columns = self._calculate_columns_for_width(available_width)
        spacing = self._calculate_horizontal_spacing_for_width(columns, available_width)
        book_ids = tuple(book.uuid for book in self._books)
        if not force and columns == self._columns and self._book_ids == book_ids:
            if spacing != self._last_horizontal_spacing:
                self._layout.setHorizontalSpacing(spacing)
                self._last_horizontal_spacing = spacing
            self._last_layout_width = available_width
            for card in self._cards.values():
                card.set_selected(card.book.uuid in self._selected_ids)
            return

        self._columns = columns
        self._book_ids = book_ids
        self._last_layout_width = available_width
        self._last_horizontal_spacing = spacing
        self._layout.setHorizontalSpacing(spacing)
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        self._cards.clear()
        for index, book in enumerate(self._books):
            card = BookCardWidget(book, self._resources)
            card.set_selected(book.uuid in self._selected_ids)
            card.book_selected.connect(self.book_selected.emit)
            card.book_opened.connect(self.book_opened.emit)
            card.detail_requested.connect(self.detail_requested.emit)
            card.menu_requested.connect(self.menu_requested.emit)
            row = index // columns
            column = index % columns
            self._layout.addWidget(card, row, column)
            self._cards[book.uuid] = card

        self._layout.addItem(
            QSpacerItem(1, 1, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding),
            (len(self._books) // columns) + 1,
            0,
            1,
            columns,
        )

    def _calculate_columns(self) -> int:
        return self._calculate_columns_for_width(self._available_row_width())

    def _calculate_columns_for_width(self, viewport_width: int) -> int:
        slot_width = Theme.book_card_width + Theme.grid_gap
        return max(1, (viewport_width + Theme.grid_gap) // slot_width)

    def _calculate_horizontal_spacing(self, columns: int) -> int:
        return self._calculate_horizontal_spacing_for_width(columns, self._available_row_width())

    def _calculate_horizontal_spacing_for_width(self, columns: int, row_width: int) -> int:
        if columns <= 1:
            return Theme.grid_gap
        justified_gap = (row_width - (columns * Theme.book_card_width)) // (columns - 1)
        return max(Theme.grid_gap, justified_gap)

    def _available_row_width(self) -> int:
        viewport_width = max(Theme.book_card_width, self.viewport().width())
        viewport_width -= Theme.content_horizontal_padding + Theme.content_scrollbar_adjusted_right_padding
        return max(Theme.book_card_width, viewport_width)

    def _should_relayout_after_resize(self) -> bool:
        available_width = self._available_row_width()
        next_columns = self._calculate_columns_for_width(available_width)
        if next_columns != self._columns:
            return True
        return abs(available_width - self._last_layout_width) >= Theme.grid_resize_relayout_buffer
