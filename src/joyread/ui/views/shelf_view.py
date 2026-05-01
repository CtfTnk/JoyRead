"""Bookshelf view binding toolbar, grid/list widgets, and ViewModel state."""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey, ShelfViewModel, ViewMode
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListWidget
from joyread.ui.widgets.menus import FigmaMenu, build_action_menu, build_book_context_menu
from joyread.ui.widgets.state_views import StateView
from joyread.ui.widgets.top_toolbar import TopToolbarWidget


class ShelfView(QWidget):
    def __init__(
        self,
        viewmodel: ShelfViewModel,
        resources: ResourceLoader,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ShelfContent")
        self._viewmodel = viewmodel

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
        self.grid = BookGridWidget(resources)
        self.list_view = BookListWidget(resources)
        self.empty_state = StateView("No books yet", "Import actions are placeholders in this phase.")
        self.loading_state = StateView("Loading library", "Preparing the mock bookshelf.")
        self.error_state = StateView("Could not load library", "The mock repository returned an error.")
        self.importing_state = StateView("Importing", "Import progress UI is reserved for a future phase.")

        for book_view in (self.grid, self.list_view):
            book_view.book_selected.connect(self._viewmodel.select_book)
            book_view.book_opened.connect(self._viewmodel.open_book)
            book_view.detail_requested.connect(self._show_detail_placeholder)
            book_view.menu_requested.connect(self._show_book_menu)

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

        self._viewmodel.state_changed.connect(self.render)
        self._viewmodel.book_open_requested.connect(self._show_read_placeholder)

    def render(self) -> None:
        self.toolbar.set_title(self._viewmodel.page_title)

        if self._viewmodel.is_loading:
            self.stack.setCurrentWidget(self.loading_state)
            return
        if self._viewmodel.error_message:
            self.stack.setCurrentWidget(self.error_state)
            return
        if self._viewmodel.is_importing:
            self.stack.setCurrentWidget(self.importing_state)
            return

        books = self._viewmodel.visible_books
        if not books:
            self.stack.setCurrentWidget(self.empty_state)
            return

        selected_ids = set(self._viewmodel.selected_book_ids)
        if self._viewmodel.view_mode == ViewMode.GRID:
            self.grid.set_books(books, selected_ids)
            self.stack.setCurrentWidget(self.grid)
        else:
            self.list_view.set_books(books, selected_ids)
            self.stack.setCurrentWidget(self.list_view)

    def _show_book_menu(self, book_uuid: str, global_pos: QPoint) -> None:
        book = self._book_by_uuid(book_uuid)
        if book is None:
            return
        menu = build_book_context_menu(
            self,
            book,
            on_read=self._viewmodel.open_book,
            on_favourite=self._viewmodel.toggle_favourite,
            on_detail=self._show_detail_placeholder,
            on_add_to_collection=lambda uuid: self._show_placeholder("Add to Collection"),
            on_remove=lambda uuid: self._show_placeholder("Remove from Library"),
            show_remove=self._viewmodel.current_shelf != ShelfKey.ALL.value,
        )
        menu.exec(global_pos)

    def _book_by_uuid(self, book_uuid: str) -> Book | None:
        for book in self._viewmodel.books:
            if book.uuid == book_uuid:
                return book
        return None

    def _show_read_placeholder(self, book_uuid: str) -> None:
        book = self._book_by_uuid(book_uuid)
        title = book.title if book else "Book"
        QMessageBox.information(self, "Read", f"Reader engine is not implemented yet.\n\n{title}")

    def _show_detail_placeholder(self, book_uuid: str) -> None:
        book = self._book_by_uuid(book_uuid)
        title = book.title if book else "Book"
        QMessageBox.information(self, "Detail", f"Detail page is reserved for a future phase.\n\n{title}")

    def _show_placeholder(self, action: str) -> None:
        QMessageBox.information(self, action, f"{action} is a placeholder in this phase.")

    def create_action_menu(self) -> FigmaMenu:
        return build_action_menu(
            self,
            on_open_book=lambda: self._show_placeholder("Open Book"),
            on_open_and_import=lambda: self._show_placeholder("Open & Import"),
            on_import=lambda: self._show_placeholder("Import"),
        )
