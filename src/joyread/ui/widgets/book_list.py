"""Basic list-mode book view."""

from __future__ import annotations

from joyread.ui.widgets.localized_text import LocalizedLabel, set_localized

from pathlib import Path

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
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.book_card import BookCoverWidget, _placeholder_cover
from joyread.ui.widgets.elided_label import ElidedLabel
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
        self._rows: dict[str, BookListRowWidget] = {}
        self._book_ids: tuple[str, ...] = ()
        self._selected_ids: set[str] = set()
        self._cover_paths: dict[str, Path] = {}

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
        self._scroll_handle = AutoHideScrollHandle(self)

    def set_books(
        self,
        books: list[Book],
        selected_ids: set[str],
        cover_paths: dict[str, Path] | None = None,
    ) -> None:
        next_cover_paths = dict(cover_paths or {})
        book_ids = tuple(book.uuid for book in books)
        wanted = set(book_ids)
        order_changed = self._book_ids != book_ids
        for book_uuid in self._rows.keys() - wanted:
            row = self._rows.pop(book_uuid)
            self._layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        for book in books:
            row = self._rows.get(book.uuid)
            if row is None:
                row = BookListRowWidget(book, self._resources)
                row.book_selected.connect(self.book_selected.emit)
                row.book_opened.connect(self.book_opened.emit)
                row.detail_requested.connect(self.detail_requested.emit)
                row.menu_requested.connect(self.menu_requested.emit)
                self._rows[book.uuid] = row
                row.set_selected(book.uuid in selected_ids)
            elif row.book != book:
                row.set_book(book)
            cover_path = next_cover_paths.get(book.uuid)
            if cover_path is not None:
                if cover_path != self._cover_paths.get(book.uuid) or not row.has_cover_path(cover_path):
                    row.set_cover_path(cover_path)
            elif book.uuid in self._cover_paths:
                row.clear_cover()
        self.set_selected_ids(selected_ids)
        self._cover_paths = next_cover_paths
        self._book_ids = book_ids
        if order_changed:
            # Detach layout items, not their widgets. Children retain ownership,
            # images and signal connections while being put in the new order.
            while self._layout.count():
                self._layout.takeAt(self._layout.count() - 1)
            for book_uuid in book_ids:
                self._layout.addWidget(self._rows[book_uuid])
            self._layout.addStretch(1)

    def set_selected_ids(self, selected_ids: set[str]) -> None:
        for book_uuid in self._selected_ids ^ selected_ids:
            row = self._rows.get(book_uuid)
            if row is not None:
                row.set_selected(book_uuid in selected_ids)
        self._selected_ids = set(selected_ids)

    def set_cover_path(self, book_uuid: str, path: Path) -> None:
        self._cover_paths[book_uuid] = path
        row = self._rows.get(book_uuid)
        if row is not None:
            row.set_cover_path(path, force=True)

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
        self._apply_availability(book.is_available)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
        )
        layout.setSpacing(Theme.spacing_md)

        self._cover = BookCoverWidget(
            _placeholder_cover(),
            QSize(Theme.book_list_cover_width, Theme.book_list_cover_height),
        )
        layout.addWidget(self._cover)

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

        self._title = title = ElidedLabel(book.title, max_lines=2)
        title.setProperty("class", "BookTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        info_layout.addWidget(title)

        self._author = author = LocalizedLabel(book.author or t("detail.unknown_author"))
        author.setProperty("class", "BookAuthor")
        author.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        set_localized(author, "setToolTip", book.author or t("detail.unknown_author"))
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

        self._progress_percent_label = LocalizedLabel(f"{book.progress_percent}%")
        self._progress_percent_label.setProperty("class", "BookProgressPercent")
        progress_unit_layout.addWidget(self._progress_percent_label)

        control_bar.addWidget(progress_unit)
        control_bar.addStretch(1)

        option_frame = QWidget()
        option_frame.setObjectName("BookOptionFrame")
        option_layout = QHBoxLayout(option_frame)
        option_layout.setContentsMargins(0, 0, 0, 0)
        option_layout.setSpacing(Theme.book_option_frame_gap)

        self._detail_button = QToolButton()
        self._detail_button.setProperty("class", "CardButton")
        self._detail_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_detail.svg"))))
        self._detail_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        self._detail_button.setFixedSize(Theme.card_button_size, Theme.card_button_size)
        self._detail_button.clicked.connect(lambda: self.detail_requested.emit(self.book.uuid))
        option_layout.addWidget(self._detail_button)

        self._option_button = QToolButton()
        self._option_button.setProperty("class", "CardButton")
        self._option_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_option.svg"))))
        self._option_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        self._option_button.setFixedSize(Theme.card_button_size, Theme.card_button_size)
        self._option_button.clicked.connect(
            lambda _checked=False, button=self._option_button: self.menu_requested.emit(
                self.book.uuid,
                button.mapToGlobal(QPoint(0, button.height())),
            )
        )
        option_layout.addWidget(self._option_button)
        self.refresh_labels()

        control_bar.addWidget(option_frame)
        info_layout.addWidget(control_bar_frame)
        layout.addWidget(content, stretch=1)

    def set_book(self, book: Book) -> None:
        previous = self.book
        self.book = book
        if previous.title != book.title:
            set_localized(self._title, "set_full_text", book.title)
        if previous.author != book.author:
            author = book.author or t("detail.unknown_author")
            set_localized(self._author, "setText", author)
            set_localized(self._author, "setToolTip", author)
        if previous.progress_percent != book.progress_percent:
            self._progress.set_progress(book.progress_percent)
            set_localized(self._progress_percent_label, "setText", f"{book.progress_percent}%")
        if previous.is_available != book.is_available:
            self._apply_availability(book.is_available)

    def _apply_availability(self, available: bool) -> None:
        if not available:
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(Theme.missing_book_opacity)
        else:
            effect = QGraphicsDropShadowEffect(self)
            effect.setBlurRadius(4)
            effect.setOffset(0, 4)
            effect.setColor(QColor(0, 0, 0, 64))
        self.setGraphicsEffect(effect)

    def set_selected(self, selected: bool) -> None:
        value = "true" if selected else "false"
        if self.property("selected") == value:
            return
        self.setProperty("selected", value)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def refresh_labels(self) -> None:
        set_localized(self._detail_button, "setToolTip", t("menu.detail"))
        set_localized(self._option_button, "setToolTip", t("detail.more_options"))

    def has_cover_path(self, path: Path) -> bool:
        return self._cover.loaded_path == path

    def clear_cover(self) -> None:
        self._cover.set_pixmap(_placeholder_cover())

    def set_cover_path(self, path: Path, *, force: bool = False) -> None:
        self._cover.set_pixmap_from_path(path, force=force)

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
