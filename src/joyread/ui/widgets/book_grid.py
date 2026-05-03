"""Responsive grid view for book cards."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLayout, QLayoutItem, QScrollArea, QWidget

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.book_card import BookCardWidget


class BookGridWidget(QScrollArea):
    book_selected = QtSignal(str, bool)
    book_opened = QtSignal(str)
    detail_requested = QtSignal(str)
    menu_requested = QtSignal(str, QPoint)
    blank_clicked = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self.setProperty("class", "ShelfScrollArea")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setObjectName("ShelfScrollViewport")
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.viewport().installEventFilter(self)
        self._books: list[Book] = []
        self._selected_ids: set[str] = set()
        self._cards: dict[str, BookCardWidget] = {}
        self._book_ids: tuple[str, ...] = ()

        self._content = QWidget()
        self._content.setObjectName("BookGridContent")
        self._content.installEventFilter(self)
        self._layout = JustifiedBookGridLayout(self._content)
        self._layout.setContentsMargins(
            Theme.content_horizontal_padding,
            Theme.grid_top_padding,
            Theme.content_scrollbar_adjusted_right_padding,
            Theme.grid_bottom_padding,
        )
        self.setWidget(self._content)
        self._scroll_handle = AutoHideScrollHandle(self)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched in (self.viewport(), self._content) and event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if isinstance(mouse_event, QMouseEvent) and mouse_event.button() == Qt.MouseButton.LeftButton:
                self.blank_clicked.emit()
        return super().eventFilter(watched, event)

    def set_books(self, books: list[Book], selected_ids: set[str]) -> None:
        self._books = list(books)
        self._selected_ids = set(selected_ids)
        book_ids = tuple(book.uuid for book in self._books)
        if self._book_ids == book_ids:
            for card in self._cards.values():
                card.set_selected(card.book.uuid in self._selected_ids)
            return

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
            self._layout.addWidget(card)
            self._cards[book.uuid] = card

        self._layout.invalidate()
        self._content.updateGeometry()

    def _calculate_columns(self) -> int:
        return self._calculate_columns_for_width(self._available_row_width())

    def _calculate_columns_for_width(self, viewport_width: int) -> int:
        return self._layout.calculate_columns_for_width(viewport_width)

    def _calculate_horizontal_spacing(self, columns: int) -> int:
        return self._calculate_horizontal_spacing_for_width(columns, self._available_row_width())

    def _calculate_horizontal_spacing_for_width(self, columns: int, row_width: int) -> int:
        return self._layout.calculate_horizontal_spacing_for_width(columns, row_width)

    def _available_row_width(self) -> int:
        viewport_width = max(Theme.book_card_width, self.viewport().width())
        viewport_width -= Theme.content_horizontal_padding + Theme.content_scrollbar_adjusted_right_padding
        return max(Theme.book_card_width, viewport_width)

    def _should_relayout_after_resize(self) -> bool:
        return False


class JustifiedBookGridLayout(QLayout):
    """Width-driven card grid with stable column thresholds and justified rows."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        left, top, right, bottom = self.getContentsMargins()
        return QSize(
            Theme.book_card_width + left + right,
            Theme.book_card_height + top + bottom,
        )

    def calculate_columns_for_width(self, available_width: int) -> int:
        if available_width <= Theme.book_card_width:
            return 1
        slot_width = Theme.book_card_width + Theme.grid_min_gap
        return max(1, int((available_width + Theme.grid_min_gap) // slot_width))

    def calculate_horizontal_spacing_for_width(self, columns: int, available_width: int) -> int:
        if columns <= 1:
            return Theme.grid_min_gap
        justified_gap = (available_width - (columns * Theme.book_card_width)) // (columns - 1)
        return max(Theme.grid_min_gap, justified_gap)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        available_width = max(Theme.book_card_width, rect.width() - left - right)
        columns = self.calculate_columns_for_width(available_width)
        horizontal_spacing = self.calculate_horizontal_spacing_for_width(columns, available_width)

        x_start = rect.x() + left
        y = rect.y() + top
        for index, item in enumerate(self._items):
            column = index % columns
            if index and column == 0:
                y += Theme.book_card_height + Theme.grid_min_gap
            x = x_start + round(column * (Theme.book_card_width + horizontal_spacing))
            if not test_only:
                item.setGeometry(QRect(x, y, Theme.book_card_width, Theme.book_card_height))

        rows = ((len(self._items) - 1) // columns) + 1 if self._items else 0
        content_height = rows * Theme.book_card_height
        if rows > 1:
            content_height += (rows - 1) * Theme.grid_min_gap
        return top + content_height + bottom
