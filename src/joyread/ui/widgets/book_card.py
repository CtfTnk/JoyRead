"""Book card widget for grid mode."""

from __future__ import annotations

from joyread.ui.widgets.localized_text import set_localized

from functools import lru_cache
from pathlib import Path

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
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.resources.pixmaps import load_thumbnail_pixmap
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.elided_label import ElidedLabel
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
        self._is_unavailable = not book.is_available
        self.setProperty("class", "BookCard")
        self.setProperty("selected", "false")
        self.setProperty("missing", "true" if book.is_missing else "false")
        self.setProperty("unavailable", "true" if book.is_unavailable else "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.book_card_width, Theme.book_card_height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._apply_unavailable_state(not book.is_available, force=True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
            Theme.book_card_layout_margin,
        )
        layout.setSpacing(Theme.book_card_gap)

        self._cover = BookCoverWidget(_placeholder_cover(), QSize(Theme.cover_width, Theme.cover_height))
        layout.addWidget(self._cover, alignment=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignTop)

        self._title = ElidedLabel(book.title, max_lines=2, reserve_full_height=True)
        self._title.setProperty("class", "BookTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._title)

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

        self._progress = BookProgressBar(book.progress_percent)
        control_bar.addWidget(self._progress)
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
            lambda _checked=False, button=self._option_button: self._emit_option_menu(button)
        )
        option_layout.addWidget(self._option_button)
        self.refresh_labels()

        control_bar.addWidget(option_frame)
        layout.addWidget(control_bar_frame)

    def set_book(self, book: Book) -> None:
        previous = self.book
        self.book = book
        if previous.title != book.title:
            set_localized(self._title, "set_full_text", book.title)
        if previous.progress_percent != book.progress_percent:
            self._progress.set_progress(book.progress_percent)
        state_changed = (previous.is_missing, previous.is_unavailable) != (book.is_missing, book.is_unavailable)
        if state_changed:
            self.setProperty("missing", "true" if book.is_missing else "false")
            self.setProperty("unavailable", "true" if book.is_unavailable else "false")
            self._apply_unavailable_state(not book.is_available, force=True)

    def refresh_labels(self) -> None:
        set_localized(self._detail_button, "setToolTip", t("menu.detail"))
        set_localized(self._option_button, "setToolTip", t("detail.more_options"))

    def set_selected(self, selected: bool) -> None:
        value = "true" if selected else "false"
        if self.property("selected") == value:
            return
        self.setProperty("selected", value)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

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

    def _emit_option_menu(self, button: QToolButton) -> None:
        self.menu_requested.emit(self.book.uuid, button.mapToGlobal(QPoint(0, button.height())))

    def _apply_unavailable_state(self, is_unavailable: bool, *, force: bool = False) -> None:
        if not force and is_unavailable == self._is_unavailable:
            return
        self._is_unavailable = is_unavailable
        if is_unavailable:
            opacity = QGraphicsOpacityEffect(self)
            opacity.setOpacity(Theme.missing_book_opacity)
            self.setGraphicsEffect(opacity)
        else:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(4)
            shadow.setOffset(0, 4)
            shadow.setColor(QColor(0, 0, 0, 64))
            self.setGraphicsEffect(shadow)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class BookCoverWidget(QFrame):
    """Cover image frame clipped to Figma's 6px corner radius."""

    def __init__(self, pixmap: QPixmap, size: QSize, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self._loaded_path: Path | None = None
        self.setObjectName("BookCover")
        self.setFixedSize(size)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self._pixmap = pixmap
        self._loaded_path = None
        self.update()

    @property
    def loaded_path(self) -> Path | None:
        return self._loaded_path

    def set_pixmap_from_path(self, path: Path, *, force: bool = False) -> None:
        if not force and self._loaded_path == path:
            return
        pixmap = load_thumbnail_pixmap(path)
        if not pixmap.isNull():
            self.set_pixmap(pixmap)
            self._loaded_path = path

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

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
