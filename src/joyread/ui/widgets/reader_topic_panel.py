"""Centered reader topic panel adapted from Figma node 612:3723."""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QContextMenuEvent, QIcon, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.reader_viewmodel import ReaderBookmarkItem, ReaderContentsItem
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.book_detail import DetailThumbnailGrid
from joyread.ui.widgets.elided_label import ElidedLabel
from joyread.ui.widgets.menus import FigmaMenu


class ReaderTopicMode(StrEnum):
    CONTENTS = "contents"
    BOOKMARKS = "bookmarks"
    THUMBNAILS = "thumbnails"


class ReaderTopicPanel(QFrame):
    thumbnail_interest_changed = QtSignal(tuple, tuple, tuple)
    thumbnail_interest_released = QtSignal()
    thumbnail_selected = QtSignal(int)
    bookmark_selected = QtSignal(int)
    contents_selected = QtSignal(int)
    new_bookmark_requested = QtSignal()
    bookmark_rename_requested = QtSignal(str, str)
    bookmark_delete_requested = QtSignal(str)

    def __init__(self, resources, parent: QWidget | None = None) -> None:  # noqa: ANN001 - ResourceLoader import cycle.
        super().__init__(parent)
        self._resources = resources
        self._mode = ReaderTopicMode.THUMBNAILS
        self._page_count = 0
        self._last_thumbnail_interest: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
        self._thumbnail_check_timer = QTimer(self)
        self._thumbnail_check_timer.setSingleShot(True)
        self._thumbnail_check_timer.timeout.connect(self._emit_thumbnail_interest)

        self.setObjectName("ReaderTopicPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(Theme.reader_topic_panel_min_width, Theme.reader_topic_panel_min_height)
        self.resize(Theme.reader_topic_panel_width, Theme.reader_topic_panel_height)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        root_layout = QVBoxLayout(self)
        # Figma uses an 8px visual padding with a 2px stroke. Qt borders
        # consume layout space, so 6px layout margins preserve the design inset.
        root_layout.setContentsMargins(
            Theme.reader_topic_panel_layout_margin,
            Theme.reader_topic_panel_layout_margin,
            Theme.reader_topic_panel_layout_margin,
            Theme.reader_topic_panel_layout_margin,
        )
        root_layout.setSpacing(Theme.reader_topic_panel_gap)

        self._stack = QStackedWidget()
        self._stack.setObjectName("ReaderTopicStack")
        root_layout.addWidget(self._stack)

        self._scroll_handles: list[AutoHideScrollHandle] = []
        self._contents_scroll = self._make_scroll_area("ReaderTopicContentsScrollArea", self._build_contents_page())
        self._bookmarks_scroll = self._make_scroll_area("ReaderTopicBookmarksScrollArea", self._build_bookmarks_page())
        self._thumbnails_scroll = self._make_scroll_area(
            "ReaderTopicThumbnailsScrollArea",
            self._build_thumbnails_page(),
        )
        thumbnail_scrollbar = self._thumbnails_scroll.verticalScrollBar()
        thumbnail_scrollbar.valueChanged.connect(self._defer_thumbnail_check)
        thumbnail_scrollbar.rangeChanged.connect(lambda _minimum, _maximum: self._defer_thumbnail_check())
        self._stack.addWidget(self._contents_scroll)
        self._stack.addWidget(self._bookmarks_scroll)
        self._stack.addWidget(self._thumbnails_scroll)
        self.set_mode(self._mode)

    @property
    def mode(self) -> ReaderTopicMode:
        return self._mode

    def set_mode(self, mode: ReaderTopicMode) -> None:
        if self._mode == ReaderTopicMode.THUMBNAILS and mode != ReaderTopicMode.THUMBNAILS:
            self._release_thumbnail_interest()
        self._mode = mode
        if mode == ReaderTopicMode.CONTENTS:
            self._stack.setCurrentWidget(self._contents_scroll)
        elif mode == ReaderTopicMode.BOOKMARKS:
            self._stack.setCurrentWidget(self._bookmarks_scroll)
        else:
            self._stack.setCurrentWidget(self._thumbnails_scroll)
            self._defer_thumbnail_check()

    def reset_thumbnails(self, page_count: int) -> None:
        self._page_count = max(0, int(page_count))
        self._thumbnail_grid.set_thumbnail_count(self._page_count, reset=True)
        self._last_thumbnail_interest = ((), ())
        self._thumbnails_scroll.verticalScrollBar().setValue(0)
        if self._mode == ReaderTopicMode.THUMBNAILS and self.isVisible():
            self._defer_thumbnail_check()

    def set_thumbnail(self, page_index: int, image_bytes: bytes) -> None:
        self._thumbnail_grid.set_thumbnail(page_index, image_bytes)

    def set_contents(self, items: tuple[ReaderContentsItem, ...]) -> None:
        """Populate CONTENTS mode with a TOC list. Empty tuple = placeholder."""
        _clear_layout(self._contents_layout)
        if not items:
            placeholder = QLabel(t("reader.toc_unavailable"))
            placeholder.setObjectName("ReaderTopicEmptyText")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            self._contents_layout.addWidget(placeholder, stretch=1)
            return
        for item in items:
            row = _TopicContentsRow(item)
            row.clicked.connect(self.contents_selected.emit)
            self._contents_layout.addWidget(row)
        self._contents_layout.addStretch(1)

    def set_bookmarks(self, bookmarks: tuple[ReaderBookmarkItem, ...]) -> None:
        _clear_layout(self._bookmark_layout)
        for bookmark in bookmarks:
            row = _TopicBookmarkRow(bookmark)
            row.clicked.connect(self.bookmark_selected.emit)
            row.rename_requested.connect(self.bookmark_rename_requested.emit)
            row.delete_requested.connect(self.bookmark_delete_requested.emit)
            self._bookmark_layout.addWidget(row)
        add_row = _NewBookmarkRow(self._resources)
        add_row.clicked.connect(self.new_bookmark_requested.emit)
        self._bookmark_layout.addWidget(add_row)
        self._bookmark_layout.addStretch(1)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._defer_thumbnail_check()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self._release_thumbnail_interest()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._defer_thumbnail_check()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        inset = Theme.reader_topic_panel_border_width / 2
        bounds = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        path.addRoundedRect(bounds, Theme.reader_topic_panel_radius, Theme.reader_topic_panel_radius)
        painter.fillPath(path, QColor(*Theme.color_menu_background_rgba))
        painter.setPen(QPen(QColor(Theme.color_button_inner_edge), Theme.reader_topic_panel_border_width))
        painter.drawPath(path)
        painter.end()

    def _build_contents_page(self) -> QWidget:
        self._contents_page = QWidget()
        self._contents_page.setObjectName("ReaderTopicContentsPage")
        self._contents_layout = QVBoxLayout(self._contents_page)
        self._contents_layout.setContentsMargins(
            Theme.reader_topic_section_padding,
            Theme.reader_topic_section_padding,
            Theme.reader_topic_section_padding,
            Theme.reader_topic_section_padding,
        )
        self._contents_layout.setSpacing(Theme.reader_topic_item_gap)
        self._contents_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # Default state: empty placeholder until a reader viewmodel supplies
        # archive folders or an EPUB table of contents.
        self.set_contents(())
        return self._contents_page

    def _build_bookmarks_page(self) -> QWidget:
        self._bookmarks_page = QWidget()
        self._bookmarks_page.setObjectName("ReaderTopicBookmarksPage")
        self._bookmark_layout = QVBoxLayout(self._bookmarks_page)
        self._bookmark_layout.setContentsMargins(
            Theme.reader_topic_section_padding,
            Theme.reader_topic_section_padding,
            Theme.reader_topic_section_padding,
            Theme.reader_topic_section_padding,
        )
        self._bookmark_layout.setSpacing(Theme.reader_topic_item_gap)
        self._bookmark_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.set_bookmarks(())
        return self._bookmarks_page

    def _build_thumbnails_page(self) -> QWidget:
        self._thumbnails_page = QWidget()
        self._thumbnails_page.setObjectName("ReaderTopicThumbnailsPage")
        layout = QVBoxLayout(self._thumbnails_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._thumbnail_grid = DetailThumbnailGrid()
        self._thumbnail_grid.thumbnail_clicked.connect(self.thumbnail_selected.emit)
        layout.addWidget(self._thumbnail_grid)
        layout.addStretch(1)
        return self._thumbnails_page

    def _make_scroll_area(self, object_name: str, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName(object_name)
        scroll.setProperty("class", "ReaderTopicScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        self._scroll_handles.append(AutoHideScrollHandle(scroll, parent=self))
        return scroll

    def _defer_thumbnail_check(self) -> None:
        if not self._thumbnail_check_timer.isActive():
            self._thumbnail_check_timer.start(0)

    def _emit_thumbnail_interest(self) -> None:
        if not self.isVisible() or self._mode != ReaderTopicMode.THUMBNAILS or self._page_count <= 0:
            return
        origin = self._thumbnail_grid.mapFrom(self._thumbnails_scroll.viewport(), QPoint(0, 0))
        viewport_rect = QRect(origin, self._thumbnails_scroll.viewport().size())
        visible, prefetch = self._thumbnail_grid.visible_and_prefetch_indices(viewport_rect, prefetch_rows=1)
        interest = (visible, prefetch)
        if interest == self._last_thumbnail_interest:
            return
        self._last_thumbnail_interest = interest
        self._thumbnail_grid.set_interest((*visible, *prefetch))
        self.thumbnail_interest_changed.emit(
            visible,
            prefetch,
            (Theme.detail_thumbnail_width, Theme.detail_thumbnail_height),
        )

    def _release_thumbnail_interest(self) -> None:
        self._thumbnail_check_timer.stop()
        self._thumbnail_grid.set_interest(())
        if self._last_thumbnail_interest != ((), ()):
            self.thumbnail_interest_released.emit()
        self._last_thumbnail_interest = ((), ())


class _TopicContentsRow(QFrame):
    clicked = QtSignal(int)

    def __init__(self, item: ReaderContentsItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._item = item
        self._pressed_inside = False
        self.setProperty("class", "ReaderTopicItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.reader_topic_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        # Indent nested TOC entries proportional to their depth, mirroring
        # how a typical book viewer renders subsections under chapters.
        depth_indent = Theme.reader_topic_item_padding_left + max(0, item.depth) * 16
        layout.setContentsMargins(
            depth_indent,
            Theme.reader_topic_item_padding_vertical,
            Theme.reader_topic_item_padding_right,
            Theme.reader_topic_item_padding_vertical,
        )
        layout.setSpacing(Theme.reader_topic_item_label_gap)

        self._label = ElidedLabel(item.label)
        self._label.setProperty("class", "ReaderTopicItemLabel")
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._label)

        index = QLabel(t("reader.page_index", index=str(item.page_index + 1)))
        index.setProperty("class", "ReaderTopicItemIndex")
        index.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(index)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit(self._item.page_index)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)


class _TopicBookmarkRow(QFrame):
    clicked = QtSignal(int)
    rename_requested = QtSignal(str, str)
    delete_requested = QtSignal(str)

    def __init__(self, bookmark: ReaderBookmarkItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bookmark = bookmark
        self._pressed_inside = False
        self.setProperty("class", "ReaderTopicItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.reader_topic_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_topic_item_padding_left,
            Theme.reader_topic_item_padding_vertical,
            Theme.reader_topic_item_padding_right,
            Theme.reader_topic_item_padding_vertical,
        )
        layout.setSpacing(Theme.reader_topic_item_label_gap)

        self._label = ElidedLabel(bookmark.name)
        self._label.setProperty("class", "ReaderTopicItemLabel")
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._label)

        index = QLabel(t("reader.page_index", index=str(bookmark.page_index + 1)))
        index.setProperty("class", "ReaderTopicItemIndex")
        index.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(index)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit(self._bookmark.page_index)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        menu = FigmaMenu(self)
        menu.add_item(
            t("reader.bookmark_rename"),
            lambda bookmark=self._bookmark: self.rename_requested.emit(bookmark.uuid, bookmark.name),
        )
        menu.add_item(
            t("reader.bookmark_delete"),
            lambda bookmark=self._bookmark: self.delete_requested.emit(bookmark.uuid),
            destructive=True,
        )
        menu.exec(event.globalPos())


class _NewBookmarkRow(QFrame):
    clicked = QtSignal()

    def __init__(self, resources, parent: QWidget | None = None) -> None:  # noqa: ANN001 - ResourceLoader import cycle.
        super().__init__(parent)
        self._pressed_inside = False
        self.setProperty("class", "ReaderTopicNewBookmark")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.reader_topic_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_topic_item_padding_left,
            Theme.reader_topic_item_padding_vertical,
            Theme.reader_topic_item_padding_right,
            Theme.reader_topic_item_padding_vertical,
        )
        layout.setSpacing(Theme.reader_topic_item_label_gap)

        icon = QLabel()
        icon.setFixedSize(Theme.reader_topic_add_icon_size, Theme.reader_topic_add_icon_size)
        icon.setPixmap(
            QIcon(str(resources.icon_path("icon_add.svg"))).pixmap(
                QSize(Theme.reader_topic_add_icon_size, Theme.reader_topic_add_icon_size)
            )
        )
        layout.addWidget(icon)

        label = QLabel(t("reader.new_bookmark"))
        label.setProperty("class", "ReaderTopicItemLabel")
        layout.addWidget(label, stretch=1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
