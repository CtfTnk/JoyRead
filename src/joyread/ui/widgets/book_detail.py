"""Floating book detail panel adapted from the Figma shelf content frame."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from math import ceil
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.core.models.book import Book
from joyread.core.models.tag import Tag
from joyread.infrastructure.i18n.locale_service import book_language_display_name, t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.book_card import BookCoverWidget, _placeholder_cover
from joyread.ui.widgets.elided_label import ElidedLabel
from joyread.ui.widgets.progress_bar import BookProgressBar
from joyread.ui.widgets.tag_selection_panel import TagChipFlowWidget


logger = logging.getLogger(__name__)


class DetailReadButton(QFrame):
    """Figma's 100x36 read button with an explicit icon/text layout frame."""

    clicked = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pressed_inside = False
        self.setProperty("class", "DetailReadButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(Theme.detail_read_button_width, Theme.detail_button_size)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(4)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 64))
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        # Figma's read button has 4px visual padding plus a 1px stroke. Qt's
        # border consumes layout space, so a 3px margin keeps the icon at x=4.
        layout.setContentsMargins(
            Theme.detail_button_layout_margin,
            Theme.detail_button_layout_margin,
            Theme.detail_button_layout_margin,
            Theme.detail_button_layout_margin,
        )
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        icon = QLabel()
        icon.setObjectName("DetailReadIcon")
        icon.setFixedSize(Theme.icon_size, Theme.icon_size)
        icon.setPixmap(
            QIcon(str(resources.icon_path("icon_read.svg"))).pixmap(QSize(Theme.icon_size, Theme.icon_size))
        )
        layout.addWidget(icon)

        self._label = QLabel()
        self._label.setProperty("class", "DetailReadButtonText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._label, stretch=1)
        self.refresh_labels()

    def refresh_labels(self) -> None:
        self._label.setText(t("menu.read"))
        self.setToolTip(t("menu.read"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class BookDetailTagBox(QFrame):
    """Figma's 160px detail tag box with the shared tag-chip scroll flow."""

    tag_clicked = QtSignal(str)
    allocation_requested = QtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BookDetailTagBox")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(Theme.detail_tag_box_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.detail_tag_box_layout_margin,
            Theme.detail_tag_box_layout_margin,
            Theme.detail_tag_box_layout_margin,
            Theme.detail_tag_box_layout_margin,
        )
        layout.setSpacing(0)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setObjectName("BookDetailTagScrollArea")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.viewport().setObjectName("BookDetailTagViewport")
        self._scroll_area.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout.addWidget(self._scroll_area)

        self._chip_flow = TagChipFlowWidget(object_name="BookDetailTagListHost")
        self._chip_flow.tag_clicked.connect(lambda tag_id, _additive: self.tag_clicked.emit(tag_id))
        self._chip_flow.add_clicked.connect(self.allocation_requested.emit)
        self._scroll_area.setWidget(self._chip_flow)
        self._scroll_handle = AutoHideScrollHandle(self._scroll_area, parent=self)

    @property
    def chip_flow(self) -> TagChipFlowWidget:
        return self._chip_flow

    def set_tags(self, tags: Iterable[Tag]) -> None:
        self._chip_flow.set_tags(tags, include_add_chip=True)

    def set_compact_title_mode(self, compact: bool) -> None:
        height = Theme.detail_tag_box_compact_height if compact else Theme.detail_tag_box_height
        if self.height() == height:
            return
        self.setFixedHeight(height)


class BookDetailPanel(QFrame):
    read_requested = QtSignal(str)
    read_at_index_requested = QtSignal(str, int)
    favourite_requested = QtSignal(str)
    menu_requested = QtSignal(str, QPoint)
    cover_edit_requested = QtSignal(str)
    thumbnail_interest_changed = QtSignal(str, tuple, tuple)
    thumbnail_interest_released = QtSignal(str)
    title_change_requested = QtSignal(str, str)
    author_change_requested = QtSignal(str, str)
    language_menu_requested = QtSignal(str, QPoint)
    tag_filter_requested = QtSignal(str, str)
    tag_allocation_requested = QtSignal(str)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self._book: Book | None = None
        self._last_thumbnail_interest: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
        self._thumbnail_interest_timer = QTimer(self)
        self._thumbnail_interest_timer.setSingleShot(True)
        self._thumbnail_interest_timer.timeout.connect(self._emit_thumbnail_interest)
        self._tag_box_sync_timer = QTimer(self)
        self._tag_box_sync_timer.setSingleShot(True)
        self._tag_box_sync_timer.timeout.connect(self._sync_tag_box_height)

        self.setObjectName("BookDetailPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root_layout = QVBoxLayout(self)
        # Figma uses an 8px visual padding with a 2px stroke. Qt borders occupy
        # layout space, so 6px margins preserve the same child inset.
        root_layout.setContentsMargins(
            Theme.detail_panel_layout_margin,
            Theme.detail_panel_layout_margin,
            Theme.detail_panel_layout_margin,
            Theme.detail_panel_layout_margin,
        )
        root_layout.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("BookDetailScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setObjectName("BookDetailViewport")
        self._scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._scroll.verticalScrollBar().valueChanged.connect(self._defer_thumbnail_interest)
        self._scroll.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, _maximum: self._defer_thumbnail_interest()
        )
        self._scroll_handle = AutoHideScrollHandle(self._scroll, parent=self)
        root_layout.addWidget(self._scroll)

        content = QWidget()
        content.setObjectName("BookDetailContent")
        content.setMinimumWidth(Theme.detail_thumbnail_min_width)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(Theme.detail_panel_gap)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._description = self._build_description()
        content_layout.addWidget(self._description)

        self._thumbnail_grid = DetailThumbnailGrid()
        self._thumbnail_grid.thumbnail_clicked.connect(self._emit_read_at_index_requested)
        content_layout.addWidget(self._thumbnail_grid)
        content_layout.addStretch(1)

        self._scroll.setWidget(content)

    def set_book(
        self,
        book: Book,
        cover_path: Path | None = None,
        tags: Iterable[Tag] = (),
    ) -> None:
        book_changed = self._book is None or self._book.uuid != book.uuid
        if book_changed:
            logger.debug("BookDetailPanel set_book book=%s title=%r", book.uuid, book.title)
        self._book = book
        self._title_field.set_value(book.title)
        self._author_field.set_value(book.author or "None", display_value=book.author or t("detail.none"))
        self._tag_box.set_tags(tags)
        self._progress.set_progress(book.progress_percent)
        self._progress_percent_label.setText(f"{book.progress_percent}%")
        self._favourite_button.setIcon(
            QIcon(
                str(
                    self._resources.icon_path(
                        "icon_favourite_enabled.svg" if book.is_favourite else "icon_favourite_cancelled.svg"
                    )
                )
            )
        )
        self.refresh_labels()
        if cover_path is not None:
            self._cover.set_pixmap_from_path(cover_path)
        elif book_changed:
            self._cover.set_pixmap(_placeholder_cover())
        if book_changed:
            self._thumbnail_grid.reset_unknown()
            self._last_thumbnail_interest = ((), ())
        self._sync_tag_box_height()
        self._defer_tag_box_height_sync()

    def refresh_labels(self) -> None:
        """Re-apply translated labels after a runtime locale change."""
        self._read_button.refresh_labels()
        self._cover.setToolTip(t("detail.edit_cover"))
        self._author_field.set_display_prefix(t("detail.author_prefix"))
        self._refresh_metadata_labels()

    def set_cover_path(self, book_uuid: str, path: Path) -> None:
        if self._book is not None and self._book.uuid == book_uuid:
            self._cover.set_pixmap_from_path(path)

    def set_page_thumbnail(self, book_uuid: str, page_index: int, image_bytes: bytes) -> None:
        if self._book is not None and self._book.uuid == book_uuid:
            self._thumbnail_grid.set_thumbnail(page_index, image_bytes)

    def set_thumbnail_page_count(self, book_uuid: str, page_count: int) -> None:
        if self._book is None or self._book.uuid != book_uuid:
            return
        self._thumbnail_grid.set_thumbnail_count(page_count, reset=True)
        self._last_thumbnail_interest = ((), ())
        self._defer_thumbnail_interest()

    def mark_thumbnail_complete(self, book_uuid: str) -> None:
        if self._book is not None and self._book.uuid == book_uuid:
            self._thumbnail_grid.mark_complete()

    def refresh_thumbnail_interest(self) -> None:
        self._last_thumbnail_interest = ((), ())
        self._defer_thumbnail_interest()

    def _build_description(self) -> QWidget:
        frame = QWidget()
        frame.setObjectName("BookDetailDescription")
        layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, frame)
        layout.setContentsMargins(
            Theme.detail_description_padding_horizontal,
            Theme.detail_description_padding_vertical,
            Theme.detail_description_padding_horizontal,
            Theme.detail_description_padding_vertical,
        )
        layout.setSpacing(Theme.detail_description_gap)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._description_layout = layout
        self._description_narrow = False

        cover_panel = QWidget()
        cover_panel.setObjectName("BookDetailCoverPanel")
        cover_layout = QVBoxLayout(cover_panel)
        cover_layout.setContentsMargins(0, 0, 0, Theme.detail_cover_panel_bottom_padding)
        cover_layout.setSpacing(Theme.detail_cover_panel_gap)
        cover_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._cover = DetailCoverWidget(QSize(Theme.detail_cover_width, Theme.detail_cover_height))
        self._cover.setToolTip(t("detail.edit_cover"))
        self._cover.double_clicked.connect(self._emit_cover_edit_requested)
        # Figma's cover panel is `items-center`; per-item alignment is needed
        # because the progress unit is narrower than the cover.
        cover_layout.addWidget(self._cover, alignment=Qt.AlignmentFlag.AlignHCenter)

        progress_unit = QWidget()
        progress_unit.setObjectName("BookDetailProgressUnit")
        progress_unit.setFixedWidth(Theme.detail_progress_unit_width)
        progress_layout = QHBoxLayout(progress_unit)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(0)
        progress_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._progress = BookProgressBar(0, width=Theme.detail_progress_width)
        progress_layout.addWidget(self._progress)
        progress_layout.addStretch(1)

        self._progress_percent_label = QLabel("0%")
        self._progress_percent_label.setProperty("class", "BookDetailProgressPercent")
        progress_layout.addWidget(self._progress_percent_label)
        cover_layout.addWidget(progress_unit, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._description_cover_panel = cover_panel

        content = QWidget()
        content.setObjectName("BookDetailDescriptionContent")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(
            Theme.detail_content_padding,
            Theme.detail_content_padding,
            Theme.detail_content_padding,
            Theme.detail_content_padding,
        )
        content_layout.setSpacing(0)

        meta = QWidget()
        meta.setObjectName("BookDetailMeta")
        meta.setFixedHeight(Theme.detail_meta_height)
        meta_layout = QVBoxLayout(meta)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(0)

        name_author = QWidget()
        name_author.setObjectName("BookDetailNameAuthor")
        name_author_layout = QVBoxLayout(name_author)
        name_author_layout.setContentsMargins(0, 0, 0, 0)
        name_author_layout.setSpacing(Theme.detail_meta_name_gap)

        self._title_field = InlineEditableText("Book Name", label_class="BookDetailTitle", max_lines=2)
        self._title_field.committed.connect(self._emit_title_change_requested)
        name_author_layout.addWidget(self._title_field)
        self._author_field = InlineEditableText(
            "None",
            label_class="BookDetailAuthor",
            display_prefix=t("detail.author_prefix"),
        )
        self._author_field.committed.connect(self._emit_author_change_requested)
        name_author_layout.addWidget(self._author_field)
        meta_layout.addWidget(name_author)
        meta_layout.addSpacing(Theme.detail_tag_box_gap)

        self._tag_box = BookDetailTagBox()
        self._tag_box.tag_clicked.connect(self._emit_tag_filter_requested)
        self._tag_box.allocation_requested.connect(self._emit_tag_allocation_requested)
        meta_layout.addWidget(self._tag_box)
        meta_layout.addSpacing(Theme.detail_tag_box_gap)

        attributes = QWidget()
        attributes.setObjectName("BookDetailAttributes")
        attributes.setFixedHeight(Theme.detail_attribute_height)
        attributes_layout = QHBoxLayout(attributes)
        attributes_layout.setContentsMargins(Theme.detail_attribute_padding_horizontal, 0, 0, 0)
        attributes_layout.setSpacing(Theme.detail_attribute_gap)
        attributes_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._language_label = DoubleClickTextLabel("")
        self._language_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._language_label.double_clicked.connect(self._emit_language_menu_requested)
        self._language_pill = _attribute_pill(self._language_label)
        self._language_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        attributes_layout.addWidget(self._language_pill)
        self._book_type_label = QLabel("")
        attributes_layout.addWidget(_attribute_pill(self._book_type_label))
        attributes_layout.addStretch(1)
        meta_layout.addWidget(attributes)

        content_layout.addWidget(meta)
        content_layout.addStretch(1)
        content_layout.addWidget(self._build_controls())

        self._description_content = content
        layout.addWidget(self._description_cover_panel)
        layout.addWidget(self._description_content, stretch=1)
        return frame

    def _build_controls(self) -> QWidget:
        frame = QWidget()
        frame.setObjectName("BookDetailControlPanel")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(
            Theme.detail_control_padding,
            Theme.detail_control_padding,
            Theme.detail_control_padding,
            Theme.detail_control_padding,
        )
        layout.setSpacing(0)

        left_side = QWidget()
        left_side.setObjectName("BookDetailControlLeftSide")
        left_layout = QHBoxLayout(left_side)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(Theme.detail_control_gap)

        self._read_button = DetailReadButton(self._resources)
        self._read_button.clicked.connect(self._emit_read_requested)
        left_layout.addWidget(self._read_button)

        self._favourite_button = QToolButton()
        self._favourite_button.setProperty("class", "DetailIconButton")
        self._favourite_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        self._favourite_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._favourite_button.setFixedSize(Theme.detail_button_size, Theme.detail_button_size)
        self._favourite_button.clicked.connect(self._emit_favourite_requested)
        left_layout.addWidget(self._favourite_button)

        layout.addWidget(left_side)
        layout.addStretch(1)

        self._option_button = QToolButton()
        self._option_button.setProperty("class", "DetailIconButton")
        self._option_button.setIcon(QIcon(str(self._resources.icon_path("icon_more_option.svg"))))
        self._option_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        self._option_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._option_button.setToolTip(t("detail.more_options"))
        self._option_button.setFixedSize(Theme.detail_button_size, Theme.detail_button_size)
        self._option_button.clicked.connect(
            lambda _checked=False, button=self._option_button: self._emit_menu_requested(button)
        )
        layout.addWidget(self._option_button)
        return frame

    def _emit_read_requested(self) -> None:
        if self._book is not None:
            self.read_requested.emit(self._book.uuid)

    def _emit_read_at_index_requested(self, page_index: int) -> None:
        if self._book is not None:
            self.read_at_index_requested.emit(self._book.uuid, page_index)

    def _emit_favourite_requested(self) -> None:
        if self._book is not None:
            self.favourite_requested.emit(self._book.uuid)

    def _emit_menu_requested(self, button: QToolButton) -> None:
        if self._book is not None:
            self.menu_requested.emit(self._book.uuid, button.mapToGlobal(QPoint(0, button.height())))

    def _emit_cover_edit_requested(self) -> None:
        if self._book is not None:
            self.cover_edit_requested.emit(self._book.uuid)

    def _defer_thumbnail_interest(self) -> None:
        if not self._thumbnail_interest_timer.isActive():
            self._thumbnail_interest_timer.start(0)

    def _emit_thumbnail_interest(self) -> None:
        if not self.isVisible() or self._book is None or self._thumbnail_grid.page_count <= 0:
            return
        origin = self._thumbnail_grid.mapFrom(self._scroll.viewport(), QPoint(0, 0))
        viewport_rect = QRect(origin, self._scroll.viewport().size())
        visible, prefetch = self._thumbnail_grid.visible_and_prefetch_indices(viewport_rect, prefetch_rows=1)
        interest = (visible, prefetch)
        if interest == self._last_thumbnail_interest:
            return
        self._last_thumbnail_interest = interest
        self._thumbnail_grid.set_interest((*visible, *prefetch))
        self.thumbnail_interest_changed.emit(self._book.uuid, visible, prefetch)

    def _emit_title_change_requested(self, title: str) -> None:
        if self._book is not None and title != self._book.title:
            self.title_change_requested.emit(self._book.uuid, title)

    def _emit_author_change_requested(self, author: str) -> None:
        if self._book is not None and author != (self._book.author or "None"):
            self.author_change_requested.emit(self._book.uuid, author)

    def _emit_language_menu_requested(self) -> None:
        if self._book is not None:
            self.language_menu_requested.emit(
                self._book.uuid,
                self._language_pill.mapToGlobal(QPoint(0, self._language_pill.height())),
            )

    def _emit_tag_filter_requested(self, tag_id: str) -> None:
        if self._book is not None:
            self.tag_filter_requested.emit(self._book.uuid, tag_id)

    def _emit_tag_allocation_requested(self) -> None:
        if self._book is not None:
            self.tag_allocation_requested.emit(self._book.uuid)

    def _sync_tag_box_height(self) -> None:
        if not hasattr(self, "_tag_box"):
            return
        self._tag_box.set_compact_title_mode(self._title_field.display_line_count > 1)

    def _defer_tag_box_height_sync(self) -> None:
        if not self._tag_box_sync_timer.isActive():
            self._tag_box_sync_timer.start(0)

    def _refresh_metadata_labels(self) -> None:
        book = self._book
        if book is None:
            self._language_label.setText(_attribute_text(t("detail.language"), t("language_name.und")))
            self._book_type_label.setText(_attribute_text(t("detail.book_type"), t("detail.book_type_unknown")))
            self._favourite_button.setToolTip(t("menu.favourite"))
            if hasattr(self, "_option_button"):
                self._option_button.setToolTip(t("detail.more_options"))
            return
        self._author_field.set_display_prefix(t("detail.author_prefix"))
        if not book.author:
            self._author_field.set_value("None", display_value=t("detail.none"))
        self._language_label.setText(
            _attribute_text(t("detail.language"), book_language_display_name(book.language_tag, book.language_name))
        )
        self._book_type_label.setText(_attribute_text(t("detail.book_type"), self._book_type_display_name(book)))
        self._favourite_button.setToolTip(t("menu.unfavourite") if book.is_favourite else t("menu.favourite"))
        if hasattr(self, "_option_button"):
            self._option_button.setToolTip(t("detail.more_options"))

    def _apply_description_layout(self, narrow: bool) -> None:
        if narrow == self._description_narrow:
            return
        logger.debug(
            "BookDetailPanel layout direction=%s width=%d",
            "vertical" if narrow else "horizontal",
            self.width(),
        )
        self._description_narrow = narrow
        layout = self._description_layout
        layout.removeWidget(self._description_cover_panel)
        layout.removeWidget(self._description_content)
        if narrow:
            layout.setDirection(QBoxLayout.Direction.TopToBottom)
            layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            layout.addWidget(self._description_cover_panel, 0, Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(self._description_content, 0)
        else:
            layout.setDirection(QBoxLayout.Direction.LeftToRight)
            layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(self._description_cover_panel, 0)
            layout.addWidget(self._description_content, 1)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._defer_tag_box_height_sync()
        self._apply_description_layout(self.width() < Theme.detail_description_narrow_threshold)
        self._defer_thumbnail_interest()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._defer_thumbnail_interest()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self._thumbnail_interest_timer.stop()
        self._thumbnail_grid.set_interest(())
        if self._book is not None:
            self.thumbnail_interest_released.emit(self._book.uuid)
        self._last_thumbnail_interest = ((), ())

    def _book_type_display_name(self, book: Book) -> str:
        return (book.file_format or t("detail.book_type_unknown")).strip().upper()


class InlineEditableText(QWidget):
    """Label that only commits Figma's inline edit when Return is pressed."""

    committed = QtSignal(str)

    def __init__(
        self,
        value: str,
        *,
        label_class: str,
        display_prefix: str = "",
        max_lines: int = 1,
    ) -> None:
        super().__init__()
        self._value = value
        self._display_value = value
        self._display_prefix = display_prefix

        layout = QStackedLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setStackingMode(QStackedLayout.StackingMode.StackOne)
        self._stack = layout

        self._label = DoubleClickLabel(max_lines=max_lines)
        self._label.setProperty("class", label_class)
        self._label.double_clicked.connect(self._begin_edit)
        layout.addWidget(self._label)

        self._editor = QLineEdit()
        self._editor.setProperty("class", "BookDetailInlineEdit")
        self._editor.installEventFilter(self)
        self._editor.returnPressed.connect(self._commit_edit)
        layout.addWidget(self._editor)
        self.set_value(value)

    def set_value(self, value: str, *, display_value: str | None = None) -> None:
        self._value = value
        self._display_value = display_value or value
        self._label.setText(f"{self._display_prefix}{self._display_value}")
        self._editor.setText(value)
        self._stack.setCurrentWidget(self._label)

    def set_display_prefix(self, prefix: str) -> None:
        self._display_prefix = prefix
        self._label.setText(f"{self._display_prefix}{self._display_value}")

    @property
    def display_line_count(self) -> int:
        return max(1, self._label.text().count("\n") + 1)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self._editor and event.type() == QEvent.Type.FocusOut:
            self._cancel_edit()
        if watched is self._editor and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:  # type: ignore[attr-defined]
                self._cancel_edit()
                return True
        return super().eventFilter(watched, event)

    def _begin_edit(self) -> None:
        self._editor.setText(self._value)
        self._stack.setCurrentWidget(self._editor)
        self._editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self._editor.selectAll()

    def _commit_edit(self) -> None:
        raw_value = self._editor.text().strip()
        value = raw_value or "None"
        self.set_value(value, display_value=raw_value or t("detail.none"))
        self.committed.emit(value)

    def _cancel_edit(self) -> None:
        self._editor.setText(self._value)
        self._stack.setCurrentWidget(self._label)


class DoubleClickLabel(ElidedLabel):
    double_clicked = QtSignal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DoubleClickTextLabel(QLabel):
    double_clicked = QtSignal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DetailCoverWidget(BookCoverWidget):
    double_clicked = QtSignal()

    def __init__(self, size: QSize) -> None:
        super().__init__(_placeholder_cover(), size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DetailThumbnailGrid(QWidget):
    thumbnail_clicked = QtSignal(int)

    def __init__(self, parent: QWidget | None = None, *, minimum_width: int | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BookDetailThumbnails")
        self.setMinimumWidth(minimum_width or Theme.detail_thumbnail_min_width)
        horizontal_policy = (
            QSizePolicy.Policy.Expanding
            if minimum_width is None
            else QSizePolicy.Policy.Minimum
        )
        self.setSizePolicy(horizontal_policy, QSizePolicy.Policy.Minimum)
        self._page_count = 0
        self._thumbnails: dict[int, DetailThumbnailWidget] = {}
        self._interest: frozenset[int] = frozenset()
        self._columns = 0
        self._is_complete = False
        self._pressed_index: int | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._margins = (
            Theme.detail_thumbnail_frame_padding + Theme.detail_thumbnail_row_padding_horizontal,
            Theme.detail_thumbnail_frame_padding + Theme.detail_thumbnail_row_padding_vertical,
            Theme.detail_thumbnail_frame_padding + Theme.detail_thumbnail_row_padding_horizontal,
            Theme.detail_thumbnail_frame_padding + Theme.detail_thumbnail_row_padding_vertical,
        )
        self._horizontal_spacing = Theme.detail_thumbnail_gap
        self._vertical_spacing = Theme.detail_thumbnail_row_gap + (Theme.detail_thumbnail_row_padding_vertical * 2)
        self._refresh_geometry(force=True)

    @property
    def page_count(self) -> int:
        return self._page_count

    def reset_unknown(self) -> None:
        self._clear_loaded_widgets()
        self._page_count = 0
        self._interest = frozenset()
        self._columns = 0
        self._is_complete = False
        self._refresh_geometry(force=True)

    def set_thumbnail_count(self, count: int, reset: bool = False) -> None:
        """Set the virtual extent without allocating one widget per page."""
        if reset:
            self.reset_unknown()
        self._page_count = max(0, int(count))
        self._clear_loaded_widgets(keep=frozenset(index for index in self._thumbnails if index < self._page_count))
        self._refresh_geometry(force=True)

    def append_thumbnail_slot(self, index: int) -> None:
        if index < 0:
            return
        if index >= self._page_count:
            self._page_count = index + 1
            self._refresh_geometry(force=True)

    def set_interest(self, indices: Iterable[int]) -> None:
        interest = frozenset(int(index) for index in indices if 0 <= int(index) < self._page_count)
        if interest == self._interest:
            return
        self._interest = interest
        self._clear_loaded_widgets(keep=interest)
        self.update()

    def visible_and_prefetch_indices(
        self,
        viewport_rect: QRect,
        *,
        prefetch_rows: int = 1,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if self._page_count <= 0 or self._columns <= 0:
            return (), ()
        visible_rect = viewport_rect.intersected(self.rect())
        if visible_rect.isEmpty():
            return (), ()
        top = self._margins[1]
        row_step = Theme.detail_thumbnail_height + self._vertical_spacing
        first_row = max(0, (visible_rect.top() - top) // row_step)
        last_row = min(
            self._row_count() - 1,
            max(first_row, (visible_rect.bottom() - top) // row_step),
        )
        visible = self._indices_for_rows(first_row, last_row)
        radius = max(0, int(prefetch_rows))
        prefetch_first = max(0, first_row - radius)
        prefetch_last = min(self._row_count() - 1, last_row + radius)
        visible_set = set(visible)
        prefetch = tuple(
            index
            for index in self._indices_for_rows(prefetch_first, prefetch_last)
            if index not in visible_set
        )
        return visible, prefetch

    def set_thumbnail(self, index: int, image_bytes: bytes) -> None:
        if not 0 <= index < self._page_count:
            return
        if self._interest and index not in self._interest:
            return
        thumbnail = self._thumbnails.get(index)
        if thumbnail is None:
            thumbnail = DetailThumbnailWidget(self)
            thumbnail.set_page_index(index)
            thumbnail.clicked.connect(self.thumbnail_clicked.emit)
            self._thumbnails[index] = thumbnail
        thumbnail.set_thumbnail_bytes(image_bytes)
        thumbnail.setGeometry(self._item_rect(index))
        thumbnail.show()
        thumbnail.raise_()
        self.update(self._item_rect(index))

    def mark_complete(self) -> None:
        self._is_complete = True

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_geometry()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        clip = event.rect().intersected(self.rect())
        if clip.isEmpty() or self._page_count <= 0:
            painter.end()
            return
        row_step = Theme.detail_thumbnail_height + self._vertical_spacing
        first_row = max(0, (clip.top() - self._margins[1]) // row_step)
        last_row = min(
            self._row_count() - 1,
            max(first_row, (clip.bottom() - self._margins[1]) // row_step),
        )
        colors = (QColor("#d8d8d8"), QColor("#cfcfcf"))
        for index in self._indices_for_rows(first_row, last_row):
            if index in self._thumbnails:
                continue
            rect = self._item_rect(index)
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), Theme.detail_thumbnail_radius, Theme.detail_thumbnail_radius)
            painter.save()
            painter.setClipPath(path)
            square = 8
            for y in range(rect.top(), rect.bottom() + 1, square):
                for x in range(rect.left(), rect.right() + 1, square):
                    color = colors[(((x - rect.left()) // square) + ((y - rect.top()) // square)) % 2]
                    painter.fillRect(x, y, square, square, color)
            painter.fillRect(rect, QColor(0, 0, 0, 28))
            painter.restore()
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_index = self._index_at(event.position().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position().toPoint())
            if index is not None and index == self._pressed_index:
                self.thumbnail_clicked.emit(index)
            self._pressed_index = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _refresh_geometry(self, force: bool = False) -> None:
        columns = self._calculate_columns()
        if not force and columns == self._columns:
            spacing = self._calculate_horizontal_spacing(columns)
            if spacing == self._horizontal_spacing:
                return
            self._horizontal_spacing = spacing
        self._columns = columns
        self._horizontal_spacing = self._calculate_horizontal_spacing(columns)
        self._position_loaded_widgets()
        self._refresh_height()
        self.update()

    def _calculate_columns(self) -> int:
        available_width = max(
            Theme.detail_thumbnail_width,
            self.width() - self._margins[0] - self._margins[2],
        )
        slot_width = Theme.detail_thumbnail_width + Theme.detail_thumbnail_gap
        return max(1, (available_width + Theme.detail_thumbnail_gap) // slot_width)

    def _calculate_horizontal_spacing(self, columns: int) -> int:
        if columns <= 1:
            return Theme.detail_thumbnail_gap
        available_width = max(
            Theme.detail_thumbnail_width,
            self.width() - self._margins[0] - self._margins[2],
        )
        justified_gap = (available_width - (columns * Theme.detail_thumbnail_width)) // (columns - 1)
        return max(Theme.detail_thumbnail_gap, justified_gap)

    def _refresh_height(self) -> None:
        rows = self._row_count()
        height = self._margins[1] + self._margins[3]
        if rows:
            height += (rows * Theme.detail_thumbnail_height) + ((rows - 1) * self._vertical_spacing)
        self.setMinimumHeight(height)

    def _row_count(self) -> int:
        return ceil(self._page_count / max(1, self._columns)) if self._page_count else 0

    def _indices_for_rows(self, first_row: int, last_row: int) -> tuple[int, ...]:
        columns = max(1, self._columns)
        start = max(0, first_row) * columns
        end = min(self._page_count, (max(first_row, last_row) + 1) * columns)
        return tuple(range(start, end))

    def _item_rect(self, index: int) -> QRect:
        columns = max(1, self._columns)
        row, column = divmod(index, columns)
        x = self._margins[0] + column * (Theme.detail_thumbnail_width + self._horizontal_spacing)
        y = self._margins[1] + row * (Theme.detail_thumbnail_height + self._vertical_spacing)
        return QRect(x, y, Theme.detail_thumbnail_width, Theme.detail_thumbnail_height)

    def _index_at(self, point: QPoint) -> int | None:
        if self._page_count <= 0:
            return None
        row_step = Theme.detail_thumbnail_height + self._vertical_spacing
        column_step = Theme.detail_thumbnail_width + self._horizontal_spacing
        local_x = point.x() - self._margins[0]
        local_y = point.y() - self._margins[1]
        if local_x < 0 or local_y < 0:
            return None
        row = local_y // row_step
        column = local_x // column_step
        if column >= max(1, self._columns):
            return None
        index = row * max(1, self._columns) + column
        return index if index < self._page_count and self._item_rect(index).contains(point) else None

    def _position_loaded_widgets(self) -> None:
        for index, thumbnail in self._thumbnails.items():
            thumbnail.setGeometry(self._item_rect(index))

    def _clear_loaded_widgets(self, keep: frozenset[int] = frozenset()) -> None:
        for index in tuple(self._thumbnails):
            if index in keep:
                continue
            widget = self._thumbnails.pop(index)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()


class DetailThumbnailWidget(QFrame):
    clicked = QtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BookDetailThumbnail")
        self.setFixedSize(Theme.detail_thumbnail_width, Theme.detail_thumbnail_height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pixmap: QPixmap | None = None
        self._page_index: int | None = None
        self._pressed_inside = False

    def set_page_index(self, page_index: int) -> None:
        self._page_index = page_index

    def set_thumbnail_bytes(self, image_bytes: bytes) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(image_bytes):
            self._pixmap = pixmap
            self.update()

    def clear_thumbnail(self) -> None:
        self._pixmap = None
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            if self.rect().contains(event.position().toPoint()) and self._page_index is not None:
                self.clicked.emit(self._page_index)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), Theme.detail_thumbnail_radius, Theme.detail_thumbnail_radius)
        painter.setClipPath(path)

        if self._pixmap is not None and not self._pixmap.isNull():
            painter.drawPixmap(self.rect(), self._pixmap)
        else:
            colors = (QColor("#d8d8d8"), QColor("#cfcfcf"))
            square = 8
            for y in range(0, self.height(), square):
                for x in range(0, self.width(), square):
                    painter.fillRect(x, y, square, square, colors[((x // square) + (y // square)) % 2])
            painter.fillRect(self.rect(), QColor(0, 0, 0, 28))
        painter.end()


def _attribute_pill(label: QLabel) -> QFrame:
    label.setProperty("class", "BookDetailPillText")
    pill = QFrame()
    pill.setProperty("class", "BookDetailPill")
    pill.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QHBoxLayout(pill)
    layout.setContentsMargins(
        Theme.detail_attribute_layout_margin,
        Theme.detail_attribute_layout_margin,
        Theme.detail_attribute_layout_margin,
        Theme.detail_attribute_layout_margin,
    )
    layout.setSpacing(0)
    layout.addWidget(label)
    return pill


def _attribute_text(label: str, value: str) -> str:
    return t("detail.attribute_format", label=label, value=value)
