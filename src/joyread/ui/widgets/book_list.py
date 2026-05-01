"""Basic list-mode book view."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QContextMenuEvent, QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_card import _placeholder_cover


class BookListWidget(QScrollArea):
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

        self._content = QWidget()
        self._content.setObjectName("BookListContent")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(
            Theme.content_horizontal_padding,
            Theme.grid_top_padding,
            Theme.content_scrollbar_adjusted_right_padding,
            Theme.grid_bottom_padding,
        )
        self._layout.setSpacing(10)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)

    def set_books(self, books: list[Book], selected_ids: set[str]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for book in books:
            row = BookListRowWidget(book, self._resources)
            row.set_selected(book.uuid in selected_ids)
            row.book_selected.connect(self.book_selected.emit)
            row.book_opened.connect(self.book_opened.emit)
            row.detail_requested.connect(self.detail_requested.emit)
            row.menu_requested.connect(self.menu_requested.emit)
            self._layout.addWidget(row)
        self._layout.addStretch(1)


class BookListRowWidget(QFrame):
    book_selected = QtSignal(str, bool)
    book_opened = QtSignal(str)
    detail_requested = QtSignal(str)
    menu_requested = QtSignal(str, QPoint)

    def __init__(self, book: Book, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.book = book
        self._resources = resources
        self.setProperty("class", "BookListRow")
        self.setProperty("selected", "false")
        self.setFixedHeight(120)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        cover = QLabel()
        cover.setObjectName("BookCover")
        cover.setFixedSize(70, 100)
        cover.setPixmap(_placeholder_cover().scaled(cover.size(), Qt.AspectRatioMode.IgnoreAspectRatio))
        layout.addWidget(cover)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(6)

        title = QLabel(book.title)
        title.setProperty("class", "BookTitle")
        title.setToolTip(book.title)
        info_layout.addWidget(title)

        author = QLabel(book.author or "Unknown author")
        author.setProperty("class", "BookMeta")
        info_layout.addWidget(author)

        meta = QLabel(f"{book.book_type} / {book.file_format}")
        meta.setProperty("class", "BookMeta")
        info_layout.addWidget(meta)

        progress = QProgressBar()
        progress.setProperty("class", "BookProgress")
        progress.setRange(0, 100)
        progress.setValue(book.progress_percent)
        progress.setFormat(f"{book.progress_percent}%")
        progress.setFixedHeight(10)
        info_layout.addWidget(progress)
        info_layout.addStretch(1)
        layout.addLayout(info_layout, stretch=1)

        detail_button = QToolButton()
        detail_button.setProperty("class", "CardButton")
        detail_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_detail.svg"))))
        detail_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        detail_button.setFixedSize(Theme.card_button_size, Theme.card_button_size)
        detail_button.setToolTip("Detail")
        detail_button.clicked.connect(lambda: self.detail_requested.emit(self.book.uuid))
        layout.addWidget(detail_button)

        option_button = QToolButton()
        option_button.setProperty("class", "CardButton")
        option_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_option.svg"))))
        option_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        option_button.setFixedSize(Theme.card_button_size, Theme.card_button_size)
        option_button.setToolTip("More options")
        option_button.clicked.connect(
            lambda _checked=False, button=option_button: self.menu_requested.emit(
                self.book.uuid,
                button.mapToGlobal(QPoint(0, button.height())),
            )
        )
        layout.addWidget(option_button)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.book_selected.emit(self.book.uuid, additive)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.book_opened.emit(self.book.uuid)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self.menu_requested.emit(self.book.uuid, event.globalPos())
