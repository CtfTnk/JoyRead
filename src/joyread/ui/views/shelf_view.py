"""Bookshelf view binding toolbar, grid/list widgets, and ViewModel state."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from joyread.core.models.book import Book
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey, ShelfViewModel, ViewMode
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListWidget
from joyread.ui.widgets.book_detail import BookDetailPanel
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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
        self.stack.setObjectName("ShelfStack")
        self.stack.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.grid = BookGridWidget(resources)
        self.list_view = BookListWidget(resources)
        self.empty_state = StateView("No books yet", "Import actions are placeholders in this phase.")
        self.loading_state = StateView("Loading library", "Preparing the mock bookshelf.")
        self.error_state = StateView("Could not load library", "The mock repository returned an error.")
        self.importing_state = StateView("Importing", "Import progress UI is reserved for a future phase.")

        for book_view in (self.grid, self.list_view):
            book_view.book_selected.connect(self._viewmodel.select_book)
            book_view.book_opened.connect(self._viewmodel.open_book)
            book_view.detail_requested.connect(self._viewmodel.show_detail)
            book_view.menu_requested.connect(self._show_book_menu)
            book_view.blank_clicked.connect(self._viewmodel.clear_selection)

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
        self.detail_panel.favourite_requested.connect(self._viewmodel.toggle_favourite)
        self.detail_panel.menu_requested.connect(self._show_book_menu)
        self.detail_panel.cover_edit_requested.connect(lambda _uuid: self._show_placeholder("Cover Editor"))

        self._viewmodel.state_changed.connect(self.render)
        self._viewmodel.book_open_requested.connect(self._show_read_placeholder)

    def render(self) -> None:
        self.toolbar.set_title(self._viewmodel.page_title)

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
            self.stack.setCurrentWidget(self.empty_state)
            self._render_detail_panel()
            return

        selected_ids = set(self._viewmodel.selected_book_ids)
        if self._viewmodel.view_mode == ViewMode.GRID:
            self.grid.set_books(books, selected_ids)
            self.stack.setCurrentWidget(self.grid)
        else:
            self.list_view.set_books(books, selected_ids)
            self.stack.setCurrentWidget(self.list_view)
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
            on_add_to_collection=lambda _uuid: self._show_placeholder_for_targets("Add to Collection", target_ids),
            on_remove=lambda _uuid: self._show_placeholder_for_targets("Remove from Library", target_ids),
            show_remove=self._viewmodel.current_shelf != ShelfKey.ALL.value,
        )
        menu.exec(global_pos)

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

    def _show_read_placeholder(self, book_uuid: str) -> None:
        book = self._book_by_uuid(book_uuid)
        title = book.title if book else "Book"
        QMessageBox.information(self, "Read", f"Reader engine is not implemented yet.\n\n{title}")

    def _show_placeholder(self, action: str) -> None:
        QMessageBox.information(self, action, f"{action} is a placeholder in this phase.")

    def _show_placeholder_for_targets(self, action: str, book_uuids: tuple[str, ...]) -> None:
        suffix = f"\n\nSelected books: {len(book_uuids)}" if len(book_uuids) > 1 else ""
        QMessageBox.information(self, action, f"{action} is a placeholder in this phase.{suffix}")

    def create_action_menu(self) -> FigmaMenu:
        return build_action_menu(
            self,
            on_open_book=lambda: self._show_placeholder("Open Book"),
            on_open_and_import=lambda: self._show_placeholder("Open & Import"),
            on_import=lambda: self._show_placeholder("Import"),
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_detail_panel()

    def _render_detail_panel(self) -> None:
        book_uuid = self._viewmodel.detail_book_uuid
        book = self._book_by_uuid(book_uuid) if book_uuid is not None else None
        if book is None:
            self.detail_panel.hide()
            return
        self.detail_panel.set_book(book)
        self._position_detail_panel()
        self.detail_panel.show()
        self.detail_panel.raise_()

    def _position_detail_panel(self) -> None:
        if not hasattr(self, "detail_panel"):
            return
        left = Theme.detail_panel_horizontal_margin
        top = Theme.detail_panel_top_margin
        width = max(0, self.width() - (left * 2))
        height = max(0, self.height() - top)
        self.detail_panel.setGeometry(left, top, width, height)
