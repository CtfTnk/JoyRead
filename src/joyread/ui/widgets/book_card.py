"""Book card widget for grid mode."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.progress_bar import BookProgressBar


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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.book_card_width, Theme.book_card_height)
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
        )
        layout.setSpacing(Theme.book_card_gap)

        cover = BookCoverWidget(_placeholder_cover(), QSize(Theme.cover_width, Theme.cover_height))
        layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignCenter)

        title = QLabel(book.title)
        title.setProperty("class", "BookTitle")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        title.setToolTip(book.title)
        layout.addWidget(title, stretch=1)

        control_bar_frame = QWidget()
        control_bar_frame.setObjectName("BookControlBar")
        control_bar_frame.setFixedHeight(Theme.book_control_bar_height)
        control_bar = QHBoxLayout(control_bar_frame)
        control_bar.setContentsMargins(
            Theme.book_control_bar_padding,
            Theme.book_control_bar_padding,
            Theme.book_control_bar_padding,
            Theme.book_control_bar_padding,
        )
        control_bar.setSpacing(0)

        progress = BookProgressBar(book.progress_percent)
        control_bar.addWidget(progress)
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
            lambda _checked=False, button=option_button: self._emit_option_menu(button)
        )
        option_layout.addWidget(option_button)

        control_bar.addWidget(option_frame)
        layout.addWidget(control_bar_frame)

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

    def _emit_option_menu(self, button: QToolButton) -> None:
        self.menu_requested.emit(self.book.uuid, button.mapToGlobal(QPoint(0, button.height())))


class BookCoverWidget(QFrame):
    """Cover image frame clipped to Figma's 6px corner radius."""

    def __init__(self, pixmap: QPixmap, size: QSize, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self.setObjectName("BookCover")
        self.setFixedSize(size)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), Theme.cover_radius, Theme.cover_radius)
        painter.setClipPath(path)
        painter.drawPixmap(self.rect(), self._pixmap)
        painter.end()


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
