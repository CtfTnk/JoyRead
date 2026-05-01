"""Bookshelf ViewModel with mock-data filtering and selection state."""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.services.library_service import LibraryService
from joyread.ui.viewmodels.signals import Signal


class ViewMode(StrEnum):
    GRID = "grid"
    LIST = "list"


class SortField(StrEnum):
    ADD_TIME = "Add Time"
    TITLE = "Title"
    AUTHOR = "Author"


class FileFilter(StrEnum):
    ALL = "All"
    COMIC = "Comic"
    NOVEL = "Novel"
    PDF = "PDF"
    EPUB = "EPUB"


class ShelfKey(StrEnum):
    ALL = "all"
    RECENT = "recent"
    FAVOURITES = "favourites"


class ShelfViewModel:
    def __init__(self, library_service: LibraryService) -> None:
        self.state_changed: Signal[None] = Signal()
        self.selection_changed: Signal[set[str]] = Signal()
        self.book_open_requested: Signal[str] = Signal()

        self._library_service = library_service
        self.books: list[Book] = []
        self.collections: list[Collection] = []
        self.search_query = ""
        self.sort_field = SortField.ADD_TIME
        self.sort_ascending = False
        self.file_filter = FileFilter.ALL
        self.view_mode = ViewMode.GRID
        self.current_shelf = ShelfKey.ALL.value
        self.selected_book_ids: set[str] = set()
        self.is_loading = False
        self.is_importing = False
        self.import_progress = 0
        self.error_message: str | None = None

    @property
    def page_title(self) -> str:
        if self.current_shelf == ShelfKey.ALL:
            return "All"
        if self.current_shelf == ShelfKey.RECENT:
            return "Recent"
        if self.current_shelf == ShelfKey.FAVOURITES:
            return "Favourites"
        collection_uuid = self._collection_uuid_from_shelf(self.current_shelf)
        for collection in self.collections:
            if collection.uuid == collection_uuid:
                return collection.name
        return "Collection"

    @property
    def visible_books(self) -> list[Book]:
        books = [book for book in self.books if self._book_in_current_shelf(book)]
        books = [book for book in books if book.matches_query(self.search_query)]
        books = [book for book in books if self._book_matches_filter(book)]
        return sorted(books, key=self._sort_key, reverse=not self.sort_ascending)

    def load_books(self) -> None:
        self.is_loading = True
        self.error_message = None
        self._emit_state()
        try:
            self.books = self._library_service.list_books()
            self.collections = self._library_service.list_collections()
        except Exception as exc:  # pragma: no cover - repository failures are not in mock path.
            self.error_message = str(exc)
            self.books = []
            self.collections = []
        finally:
            self.is_loading = False
            self._emit_state()

    def set_current_shelf(self, shelf: str) -> None:
        if shelf == self.current_shelf:
            return
        self.current_shelf = shelf
        self.clear_selection(emit_state=False)
        self._emit_state()

    def set_search_query(self, query: str) -> None:
        if query == self.search_query:
            return
        self.search_query = query
        self.clear_selection(emit_state=False)
        self._emit_state()

    def set_sort(self, field: str, ascending: bool | None = None) -> None:
        normalized_field = SortField(field)
        changed = normalized_field != self.sort_field
        self.sort_field = normalized_field
        if ascending is not None and ascending != self.sort_ascending:
            self.sort_ascending = ascending
            changed = True
        if changed:
            self._emit_state()

    def set_filter(self, filter_name: str) -> None:
        normalized_filter = FileFilter(filter_name)
        if normalized_filter == self.file_filter:
            return
        self.file_filter = normalized_filter
        self.clear_selection(emit_state=False)
        self._emit_state()

    def set_view_mode(self, mode: str) -> None:
        normalized_mode = ViewMode(mode)
        if normalized_mode == self.view_mode:
            return
        self.view_mode = normalized_mode
        self._emit_state()

    def select_book(self, book_uuid: str, additive: bool = False) -> None:
        if additive:
            if book_uuid in self.selected_book_ids:
                self.selected_book_ids.remove(book_uuid)
            else:
                self.selected_book_ids.add(book_uuid)
        else:
            self.selected_book_ids = {book_uuid}
        self.selection_changed.emit(set(self.selected_book_ids))
        self._emit_state()

    def toggle_book_selection(self, book_uuid: str) -> None:
        self.select_book(book_uuid, additive=True)

    def clear_selection(self, emit_state: bool = True) -> None:
        if not self.selected_book_ids:
            return
        self.selected_book_ids.clear()
        self.selection_changed.emit(set())
        if emit_state:
            self._emit_state()

    def open_book(self, book_uuid: str) -> None:
        if any(book.uuid == book_uuid for book in self.books):
            self.book_open_requested.emit(book_uuid)

    def toggle_favourite(self, book_uuid: str) -> None:
        changed = False
        next_books: list[Book] = []
        for book in self.books:
            if book.uuid == book_uuid:
                next_books.append(book.with_favourite(not book.is_favourite))
                changed = True
            else:
                next_books.append(book)
        if changed:
            self.books = next_books
            self._emit_state()

    def _book_in_current_shelf(self, book: Book) -> bool:
        if self.current_shelf == ShelfKey.ALL:
            return True
        if self.current_shelf == ShelfKey.RECENT:
            return book.last_read_at is not None
        if self.current_shelf == ShelfKey.FAVOURITES:
            return book.is_favourite
        collection_uuid = self._collection_uuid_from_shelf(self.current_shelf)
        return collection_uuid in book.collection_ids

    def _book_matches_filter(self, book: Book) -> bool:
        if self.file_filter == FileFilter.ALL:
            return True
        if self.file_filter == FileFilter.COMIC:
            return book.book_type.lower() == "comic"
        if self.file_filter == FileFilter.NOVEL:
            return book.book_type.lower() == "novel"
        return book.file_format.upper() == self.file_filter.value.upper()

    def _sort_key(self, book: Book) -> tuple[object, str]:
        if self.current_shelf == ShelfKey.RECENT:
            return (book.last_read_at is None, book.last_read_at or book.added_at, book.title.lower())
        if self.sort_field == SortField.TITLE:
            return (book.title.lower(), book.uuid)
        if self.sort_field == SortField.AUTHOR:
            return ((book.author or "").lower(), book.title.lower())
        return (book.added_at, book.title.lower())

    def _collection_uuid_from_shelf(self, shelf: str) -> str:
        prefix = "collection:"
        return shelf[len(prefix) :] if shelf.startswith(prefix) else shelf

    def _emit_state(self) -> None:
        visible_ids = {book.uuid for book in self.visible_books}
        removed = self.selected_book_ids - visible_ids
        if removed:
            self.selected_book_ids -= removed
            self.selection_changed.emit(set(self.selected_book_ids))
        self.state_changed.emit()


def collection_shelf_key(collection_uuid: str) -> str:
    return f"collection:{collection_uuid}"
