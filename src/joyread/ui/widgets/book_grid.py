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
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._books: list[Book] = []
        self._selected_ids: set[str] = set()
        self._cards: dict[str, BookCardWidget] = {}
        self._book_ids: tuple[str, ...] = ()
        self._columns = 0

        self._content = QWidget()
        self._content.setObjectName("BookGridContent")
        self._layout = QGridLayout(self._content)
        self._layout.setContentsMargins(0, 4, 0, 24)
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
        self._relayout()

    def _relayout(self, force: bool = False) -> None:
        columns = self._calculate_columns()
        book_ids = tuple(book.uuid for book in self._books)
        if not force and columns == self._columns and self._book_ids == book_ids:
            for card in self._cards.values():
                card.set_selected(card.book.uuid in self._selected_ids)
            return

        self._columns = columns
        self._book_ids = book_ids
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
        viewport_width = max(Theme.book_card_width, self.viewport().width())
        slot_width = Theme.book_card_width + Theme.grid_gap
        return max(1, (viewport_width + Theme.grid_gap) // slot_width)
