"""Book card widget for grid mode."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QContextMenuEvent, QIcon, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
)

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme


class BookCardWidget(QFrame):
    book_selected = QtSignal(str, bool)
    book_opened = QtSignal(str)
    detail_requested = QtSignal(str)
    menu_requested = QtSignal(str, QPoint)

    def __init__(self, book: Book, resources: ResourceLoader, parent: QFrame | None = None) -> None:
        super().__init__(parent)
        self.book = book
        self._resources = resources
        self.setProperty("class", "BookCard")
        self.setProperty("selected", "false")
        self.setProperty("missing", "true" if book.is_missing else "false")
        self.setFixedSize(Theme.book_card_width, Theme.book_card_height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(8)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 60))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        cover = QLabel()
        cover.setObjectName("BookCover")
        cover.setFixedSize(Theme.cover_width, Theme.cover_height)
        cover.setPixmap(_placeholder_cover().scaled(cover.size(), Qt.AspectRatioMode.IgnoreAspectRatio))
        cover.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignCenter)

        title = QLabel(book.title)
        title.setProperty("class", "BookTitle")
        title.setWordWrap(True)
        title.setFixedHeight(36)
        title.setToolTip(book.title)
        layout.addWidget(title)

        control_bar = QHBoxLayout()
        control_bar.setContentsMargins(2, 0, 2, 0)
        control_bar.setSpacing(4)

        progress = QProgressBar()
        progress.setProperty("class", "BookProgress")
        progress.setRange(0, 100)
        progress.setValue(book.progress_percent)
        progress.setTextVisible(False)
        progress.setFixedSize(65, 10)
        control_bar.addWidget(progress)
        control_bar.addStretch(1)

        detail_button = QToolButton()
        detail_button.setProperty("class", "CardButton")
        detail_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_detail.svg"))))
        detail_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        detail_button.setFixedSize(Theme.card_button_size, Theme.card_button_size)
        detail_button.setToolTip("Detail")
        detail_button.clicked.connect(lambda: self.detail_requested.emit(self.book.uuid))
        control_bar.addWidget(detail_button)

        option_button = QToolButton()
        option_button.setProperty("class", "CardButton")
        option_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_option.svg"))))
        option_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        option_button.setFixedSize(Theme.card_button_size, Theme.card_button_size)
        option_button.setToolTip("More options")
        option_button.clicked.connect(
            lambda _checked=False, button=option_button: self._emit_option_menu(button)
        )
        control_bar.addWidget(option_button)

        layout.addLayout(control_bar)

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

    def _emit_option_menu(self, button: QToolButton) -> None:
        self.menu_requested.emit(self.book.uuid, button.mapToGlobal(QPoint(0, button.height())))


@lru_cache(maxsize=1)
def _placeholder_cover() -> QPixmap:
    pixmap = QPixmap(Theme.cover_width, Theme.cover_height)
    painter = QPainter(pixmap)
    colors = (QColor("#fafafa"), QColor("#efefef"))
    square = 12
    for y in range(0, Theme.cover_height, square):
        for x in range(0, Theme.cover_width, square):
            painter.fillRect(x, y, square, square, colors[((x // square) + (y // square)) % 2])
    painter.end()
    return pixmap
