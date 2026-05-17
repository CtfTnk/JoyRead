"""Bookshelf view binding toolbar, grid/list widgets, and ViewModel state."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import QKeyEvent, QKeySequence, QMouseEvent, QResizeEvent, QShortcut
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey, ShelfViewModel, ViewMode
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListWidget
from joyread.ui.widgets.book_detail import BookDetailPanel
from joyread.ui.widgets.menus import (
    FigmaMenu,
    build_action_menu,
    build_book_context_menu,
    build_language_dropdown_menu,
)
from joyread.ui.widgets.state_views import StateView
from joyread.ui.widgets.top_toolbar import TopToolbarWidget


class ShelfView(QWidget):
    info_requested = QtSignal(str, str)
    import_requested = QtSignal()
    delete_books_requested = QtSignal(tuple)
    add_to_collection_requested = QtSignal(tuple)
    export_books_requested = QtSignal(tuple)
    # ``read_book_*`` decisions are emitted by ShelfViewModel directly
    # (``book_open_requested`` / ``book_open_at_requested``). MainWindow
    # subscribes to the VM so every open is gated by the VM's
    # ``_refresh_book_state`` check, and we don't need a view-layer
    # relay to bridge them.
    open_file_requested = QtSignal(bool)

    def __init__(
        self,
        viewmodel: ShelfViewModel,
        resources: ResourceLoader,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ShelfContent")
        self.setProperty("sidebarVisible", "true")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._viewmodel = viewmodel
        self._resources = resources
        # Streaming thumbnail/cover updates are paused while a popup
        # (context menu, dialog) is open and replayed on close. Live grid
        # mutations during a popup interaction cause jank — items reflow
        # while the user is mid-click — so we coalesce updates here and
        # flush them once the depth counter returns to zero.
        self._popup_interaction_depth = 0
        self._deferred_page_thumbnails: list[tuple[str, int, bytes]] = []
        self._deferred_detail_batch_finishes: list[tuple[str, int, bool]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            0,
            Theme.content_top_padding,
            0,
            0,
        )
        layout.setSpacing(10)

        self.toolbar = TopToolbarWidget(resources)
        self.toolbar.search_changed.connect(self._viewmodel.set_search_query)
        self.toolbar.filter_changed.connect(self._viewmodel.set_filter)
        layout.addWidget(self.toolbar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("ShelfStack")
        self.stack.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.grid = BookGridWidget(resources)
        self.list_view = BookListWidget(resources)
        self.empty_state = StateView("No books yet", "Use Open & Import or Import to add books to your bookshelf.")
        self.loading_state = StateView("Loading library", "Preparing the mock bookshelf.")
        self.error_state = StateView("Could not load library", "The library service returned an error.")
        self.importing_state = StateView("Importing", "Import progress UI is reserved for a future phase.")

        for book_view in (self.grid, self.list_view):
            book_view.book_selected.connect(self._viewmodel.select_book)
            book_view.book_opened.connect(self._viewmodel.open_book)
            book_view.detail_requested.connect(self._viewmodel.show_detail)
            book_view.menu_requested.connect(self._show_book_menu)
            book_view.blank_clicked.connect(self._handle_blank_clicked)

        for widget in (
            self.grid,
            self.list_view,
            self.empty_state,
            self.loading_state,
            self.error_state,
            self.importing_state,
        ):
            self.stack.addWidget(widget)
        layout.addWidget(self.stack, stretch=1)

        self.detail_panel = BookDetailPanel(resources, self)
        self.detail_panel.hide()
        self.detail_panel.read_requested.connect(self._viewmodel.open_book)
        self.detail_panel.read_at_index_requested.connect(self._viewmodel.open_book_at)
        self.detail_panel.favourite_requested.connect(self._viewmodel.toggle_favourite)
        self.detail_panel.menu_requested.connect(self._show_book_menu)
        self.detail_panel.cover_edit_requested.connect(lambda _uuid: self._show_placeholder("Cover Editor"))
        self.detail_panel.more_thumbnails_requested.connect(self._request_next_detail_thumbnail_batch)
        self.detail_panel.title_change_requested.connect(self._viewmodel.update_book_title)
        self.detail_panel.author_change_requested.connect(self._viewmodel.update_book_author)
        self.detail_panel.language_menu_requested.connect(self._show_language_menu)

        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self._viewmodel.hide_detail)

        self._viewmodel.state_changed.connect(self.render)
        self._viewmodel.cover_ready.connect(self._handle_cover_ready)
        self._viewmodel.page_thumbnail_ready.connect(self._handle_page_thumbnail_ready)
        self._viewmodel.detail_thumbnail_batch_finished.connect(self._handle_detail_thumbnail_batch_finished)

    def render(self) -> None:
        self.toolbar.set_title(self._viewmodel.page_title)
        self.toolbar.set_filter(self._viewmodel.file_filter.value)

        if self._viewmodel.is_loading:
            self.stack.setCurrentWidget(self.loading_state)
            self._render_detail_panel()
            return
        if self._viewmodel.error_message:
            self.stack.setCurrentWidget(self.error_state)
            self._render_detail_panel()
            return
        if self._viewmodel.is_importing:
            self.stack.setCurrentWidget(self.importing_state)
            self._render_detail_panel()
            return

        books = self._viewmodel.visible_books
        if not books:
            self._update_empty_state_copy()
            self.stack.setCurrentWidget(self.empty_state)
            self._render_detail_panel()
            return

        selected_ids = set(self._viewmodel.selected_book_ids)
        cover_paths = self._viewmodel.cover_paths
        if self._viewmodel.view_mode == ViewMode.GRID:
            self.grid.set_books(books, selected_ids, cover_paths)
            self.stack.setCurrentWidget(self.grid)
        else:
            self.list_view.set_books(books, selected_ids, cover_paths)
            self.stack.setCurrentWidget(self.list_view)
        visible_ids = tuple(book.uuid for book in books)
        QTimer.singleShot(0, lambda visible_ids=visible_ids: self._viewmodel.request_covers_for_books(visible_ids))
        self._render_detail_panel()

    def _show_book_menu(self, book_uuid: str, global_pos: QPoint) -> None:
        book = self._book_by_uuid(book_uuid)
        if book is None:
            return
        target_ids = self._menu_target_ids(book_uuid)
        next_favourite_state = not book.is_favourite
        menu = build_book_context_menu(
            self,
            book,
            on_read=self._viewmodel.open_book,
            on_favourite=lambda _uuid: self._viewmodel.set_favourite(target_ids, next_favourite_state),
            on_detail=self._viewmodel.show_detail,
            on_add_to_collection=lambda _uuid: self.add_to_collection_requested.emit(target_ids),
            on_export=lambda _uuid: self.export_books_requested.emit(target_ids),
            on_remove=lambda _uuid: self._viewmodel.remove_books_from_current_shelf(target_ids),
            on_delete=lambda _uuid: self.delete_books_requested.emit(target_ids),
            show_remove=self._viewmodel.can_remove_from_current_shelf,
        )
        self._exec_interaction_popup(menu, global_pos)

    def _show_language_menu(self, book_uuid: str, global_pos: QPoint) -> None:
        book = self._book_by_uuid(book_uuid)
        if book is None:
            return
        menu = build_language_dropdown_menu(
            self,
            self._resources,
            self._viewmodel.languages,
            book.language_tag,
            lambda language_tag: self._viewmodel.update_book_language(book_uuid, language_tag),
        )
        self._exec_interaction_popup(menu, global_pos)

    def _menu_target_ids(self, book_uuid: str) -> tuple[str, ...]:
        selected_ids = set(self._viewmodel.selected_book_ids)
        if book_uuid in selected_ids:
            return tuple(book.uuid for book in self._viewmodel.visible_books if book.uuid in selected_ids)
        self._viewmodel.select_book(book_uuid)
        return (book_uuid,)

    def _book_by_uuid(self, book_uuid: str) -> Book | None:
        for book in self._viewmodel.books:
            if book.uuid == book_uuid:
                return book
        return None

    def _update_empty_state_copy(self) -> None:
        if self._viewmodel.search_query or self._viewmodel.file_filter.value != "ALL":
            self.empty_state.set_text("No matching books", "Try adjusting your search or filter.")
            return
        if self._viewmodel.current_shelf == ShelfKey.ALL.value:
            self.empty_state.set_text(
                "No books yet",
                "Use Open & Import or Import to add books to your bookshelf.",
            )
            return
        if self._viewmodel.current_shelf == ShelfKey.RECENT.value:
            self.empty_state.set_text("No recent books", "Books you read will appear here.")
            return
        if self._viewmodel.current_shelf == ShelfKey.FAVOURITES.value:
            self.empty_state.set_text(
                "No favourites yet",
                "Favourite books from a book's More menu to find them here.",
            )
            return
        if self._viewmodel.current_shelf.startswith("collection:"):
            self.empty_state.set_text(
                "No books in this collection",
                "Add books to this collection from a book's More menu.",
            )
            return
        self.empty_state.set_text("No books found", "There are no books to show here.")

    def _show_placeholder(self, action: str) -> None:
        self.info_requested.emit(action, f"{action} is a placeholder in this phase.")

    def create_action_menu(self) -> FigmaMenu:
        return build_action_menu(
            self,
            on_open_book=lambda: self.open_file_requested.emit(False),
            on_open_and_import=lambda: self.open_file_requested.emit(True),
            on_import=self.import_requested.emit,
        )

    def set_sidebar_visible(self, visible: bool) -> None:
        self.setProperty("sidebarVisible", "true" if visible else "false")
        # QSS border-radius paints each widget independently. When the sidebar
        # is hidden, ShelfContent and its scroll child become the left-edge
        # painters, so they need their own bottom-left radius.
        for widget in (self, self.stack, self.grid, self.grid.viewport(), self.list_view, self.list_view.viewport()):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_detail_panel()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.detail_panel.isVisible()
            and not self.detail_panel.geometry().contains(event.position().toPoint())
        ):
            self._handle_blank_clicked()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self._viewmodel.detail_book_uuid is not None:
            self._viewmodel.hide_detail()
            event.accept()
            return
        super().keyPressEvent(event)

    def _handle_blank_clicked(self) -> None:
        if self._viewmodel.detail_book_uuid is not None:
            # Preserve the existing blank-click deselection behavior while
            # using the same gesture as the natural way to dismiss detail.
            self._viewmodel.clear_selection(emit_state=False)
            self._viewmodel.hide_detail()
            return
        self._viewmodel.clear_selection()

    def _render_detail_panel(self) -> None:
        book_uuid = self._viewmodel.detail_book_uuid
        book = self._book_by_uuid(book_uuid) if book_uuid is not None else None
        if book is None:
            self.detail_panel.hide()
            return
        self.detail_panel.set_book(book, self._viewmodel.cover_paths.get(book.uuid))
        self._position_detail_panel()
        self.detail_panel.show()
        self.detail_panel.raise_()
        if not self._is_popup_interaction_active():
            QTimer.singleShot(0, lambda book_uuid=book.uuid: self._request_next_detail_thumbnail_batch(book_uuid))

    def _position_detail_panel(self) -> None:
        if not hasattr(self, "detail_panel"):
            return
        left = Theme.detail_panel_horizontal_margin
        top = Theme.detail_panel_top_margin
        width = max(0, self.width() - (left * 2))
        height = max(0, self.height() - top)
        self.detail_panel.setGeometry(left, top, width, height)

    def _handle_cover_ready(self, book_uuid: str, path) -> None:
        self.grid.set_cover_path(book_uuid, path)
        self.list_view.set_cover_path(book_uuid, path)
        self.detail_panel.set_cover_path(book_uuid, path)

    def _handle_page_thumbnail_ready(self, book_uuid: str, page_index: int, image_bytes: bytes) -> None:
        if self._is_popup_interaction_active():
            self._deferred_page_thumbnails.append((book_uuid, page_index, image_bytes))
            return
        self.detail_panel.set_page_thumbnail(book_uuid, page_index, image_bytes)

    def _handle_detail_thumbnail_batch_finished(self, book_uuid: str, _next_index: int, has_more: bool) -> None:
        if self._is_popup_interaction_active():
            self._deferred_detail_batch_finishes.append((book_uuid, _next_index, has_more))
            return
        self._apply_detail_thumbnail_batch_finished(book_uuid, has_more)

    def _apply_detail_thumbnail_batch_finished(self, book_uuid: str, has_more: bool) -> None:
        if not has_more:
            self.detail_panel.mark_thumbnail_complete(book_uuid)
            return
        if self.detail_panel.isVisible() and self.detail_panel.is_near_thumbnail_bottom():
            QTimer.singleShot(0, lambda book_uuid=book_uuid: self._request_next_detail_thumbnail_batch(book_uuid))

    def _request_next_detail_thumbnail_batch(self, book_uuid: str) -> None:
        if self._is_popup_interaction_active():
            return
        self._viewmodel.request_next_detail_thumbnail_batch(
            book_uuid,
            (Theme.detail_thumbnail_width, Theme.detail_thumbnail_height),
        )

    def _exec_interaction_popup(self, menu: FigmaMenu, global_pos: QPoint) -> None:
        self._popup_interaction_depth += 1
        try:
            menu.exec(global_pos)
        finally:
            self._popup_interaction_depth = max(0, self._popup_interaction_depth - 1)
            if not self._is_popup_interaction_active():
                applied_deferred_batch_finish = self._flush_deferred_detail_thumbnail_updates()
                if not applied_deferred_batch_finish:
                    self._resume_detail_thumbnail_loading_if_needed()

    def _is_popup_interaction_active(self) -> bool:
        return self._popup_interaction_depth > 0

    def _flush_deferred_detail_thumbnail_updates(self) -> bool:
        current_book_uuid = self._viewmodel.detail_book_uuid
        page_updates = self._deferred_page_thumbnails
        batch_finishes = self._deferred_detail_batch_finishes
        self._deferred_page_thumbnails = []
        self._deferred_detail_batch_finishes = []
        if current_book_uuid is None:
            return False

        for book_uuid, page_index, image_bytes in page_updates:
            if book_uuid == current_book_uuid:
                self.detail_panel.set_page_thumbnail(book_uuid, page_index, image_bytes)

        current_finishes = [
            (book_uuid, has_more)
            for book_uuid, _next_index, has_more in batch_finishes
            if book_uuid == current_book_uuid
        ]
        if not current_finishes:
            return False
        _, has_more = current_finishes[-1]
        self._apply_detail_thumbnail_batch_finished(current_book_uuid, has_more)
        return True

    def _resume_detail_thumbnail_loading_if_needed(self) -> None:
        book_uuid = self._viewmodel.detail_book_uuid
        if (
            book_uuid is not None
            and self.detail_panel.isVisible()
            and self.detail_panel.is_near_thumbnail_bottom()
        ):
            QTimer.singleShot(0, lambda book_uuid=book_uuid: self._request_next_detail_thumbnail_batch(book_uuid))
