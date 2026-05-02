"""Basic list-mode book view."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QContextMenuEvent, QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_card import BookCoverWidget, _placeholder_cover
from joyread.ui.widgets.progress_bar import BookProgressBar


class BookListWidget(QScrollArea):
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

        self._content = QWidget()
        self._content.setObjectName("BookListContent")
        self._content.installEventFilter(self)
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

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched in (self.viewport(), self._content) and event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if isinstance(mouse_event, QMouseEvent) and mouse_event.button() == Qt.MouseButton.LeftButton:
                self.blank_clicked.emit()
        return super().eventFilter(watched, event)


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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(Theme.book_list_row_height)
        self.setMinimumWidth(Theme.book_list_row_width)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if book.is_missing:
            opacity = QGraphicsOpacityEffect(self)
            opacity.setOpacity(Theme.missing_book_opacity)
            self.setGraphicsEffect(opacity)
        else:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(4)
            shadow.setOffset(0, 4)
            shadow.setColor(QColor(0, 0, 0, 64))
            self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
        )
        layout.setSpacing(Theme.spacing_md)

        cover = BookCoverWidget(
            _placeholder_cover(),
            QSize(Theme.book_list_cover_width, Theme.book_list_cover_height),
        )
        layout.addWidget(cover)

        content = QWidget()
        content.setObjectName("BookListRowContent")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        info_layout = QVBoxLayout(content)
        info_layout.setContentsMargins(
            Theme.book_list_content_padding_horizontal,
            0,
            Theme.book_list_content_padding_horizontal,
            0,
        )
        info_layout.setSpacing(0)

        title = QLabel(book.title)
        title.setProperty("class", "BookTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        title.setToolTip(book.title)
        info_layout.addWidget(title)

        author = QLabel(book.author or "Unknown author")
        author.setProperty("class", "BookAuthor")
        author.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        author.setToolTip(author.text())
        info_layout.addWidget(author)

        info_layout.addStretch(1)

        control_bar_frame = QWidget()
        control_bar_frame.setObjectName("BookListControlBar")
        control_bar_frame.setFixedHeight(Theme.book_control_bar_height)
        control_bar = QHBoxLayout(control_bar_frame)
        control_bar.setContentsMargins(
            Theme.book_control_bar_padding,
            Theme.book_control_bar_padding,
            Theme.book_control_bar_padding,
            Theme.book_control_bar_padding,
        )
        control_bar.setSpacing(0)

        progress_unit = QWidget()
        progress_unit.setObjectName("BookProgressUnit")
        progress_unit_layout = QHBoxLayout(progress_unit)
        progress_unit_layout.setContentsMargins(0, 0, 0, 0)
        progress_unit_layout.setSpacing(Theme.book_progress_percent_gap)
        progress_unit_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._progress = BookProgressBar(book.progress_percent)
        progress_unit_layout.addWidget(self._progress)

        self._progress_percent_label = QLabel(f"{book.progress_percent}%")
        self._progress_percent_label.setProperty("class", "BookProgressPercent")
        progress_unit_layout.addWidget(self._progress_percent_label)

        control_bar.addWidget(progress_unit)
        control_bar.addStretch(1)

        option_frame = QWidget()
        option_frame.setObjectName("BookOptionFrame")
        option_layout = QHBoxLayout(option_frame)
        option_layout.setContentsMargins(0, 0, 0, 0)
        option_layout.setSpacing(Theme.book_option_frame_gap)

        detail_button = QToolButton()
        detail_button.setProperty("class", "CardButton")
        detail_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_detail.svg"))))
        detail_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        detail_button.setFixedSize(Theme.card_button_size, Theme.card_button_size)
        detail_button.setToolTip("Detail")
        detail_button.clicked.connect(lambda: self.detail_requested.emit(self.book.uuid))
        option_layout.addWidget(detail_button)

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
        option_layout.addWidget(option_button)

        control_bar.addWidget(option_frame)
        info_layout.addWidget(control_bar_frame)
        layout.addWidget(content, stretch=1)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.book_selected.emit(self.book.uuid, additive)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.book_opened.emit(self.book.uuid)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self.menu_requested.emit(self.book.uuid, event.globalPos())
