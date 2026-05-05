"""Bookshelf ViewModel with mock-data filtering and selection state."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.services.library_service import LibraryService
from joyread.core.services.task_service import TaskService
from joyread.core.services.thumbnail_service import ThumbnailService
from joyread.ui.viewmodels.signals import Signal


class ViewMode(StrEnum):
    GRID = "grid"
    LIST = "list"


class SortField(StrEnum):
    ADD_TIME = "Add Time"
    TITLE = "Title"
    AUTHOR = "Author"


class FileFilter(StrEnum):
    ALL = "ALL"
    CBZ = "CBZ"
    CBR = "CBR"
    ZIP = "ZIP"
    RAR = "RAR"
    SEVEN_Z = "7Z"
    PDF = "PDF"
    EPUB = "EPUB"


class ShelfKey(StrEnum):
    ALL = "all"
    RECENT = "recent"
    FAVOURITES = "favourites"


class ShelfViewModel:
    def __init__(
        self,
        library_service: LibraryService,
        thumbnail_service: ThumbnailService | None = None,
        task_service: TaskService | None = None,
        cover_size: tuple[int, int] | None = None,
    ) -> None:
        self.state_changed: Signal[None] = Signal()
        self.selection_changed: Signal[set[str]] = Signal()
        self.book_open_requested: Signal[str] = Signal()
        self.cover_ready: Signal[tuple[str, Path]] = Signal()
        self.page_thumbnail_ready: Signal[tuple[str, int, bytes]] = Signal()

        self._library_service = library_service
        self._thumbnail_service = thumbnail_service
        self._task_service = task_service
        self._cover_size = cover_size
        self.books: list[Book] = []
        self.collections: list[Collection] = []
        self.search_query = ""
        self.sort_field = SortField.ADD_TIME
        self.sort_ascending = False
        self.file_filter = FileFilter.ALL
        self.view_mode = ViewMode.GRID
        self.current_shelf = ShelfKey.ALL.value
        self.selected_book_ids: set[str] = set()
        self.detail_book_uuid: str | None = None
        self.is_loading = False
        self.is_importing = False
        self.import_progress = 0
        self.error_message: str | None = None
        self._cover_paths: dict[str, Path] = {}
        self._pending_cover_ids: set[str] = set()
        self._page_thumbnail_bytes: dict[tuple[str, int, tuple[int, int]], bytes] = {}
        self._pending_page_thumbnail_keys: set[tuple[str, int, tuple[int, int]]] = set()

    @property
    def cover_paths(self) -> dict[str, Path]:
        return dict(self._cover_paths)

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
        if self.error_message is None:
            self.request_cover_generation_for_loaded_books()

    def set_current_shelf(self, shelf: str) -> None:
        if shelf == self.current_shelf:
            return
        self.current_shelf = shelf
        self.clear_selection(emit_state=False)
        self.detail_book_uuid = None
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

    def show_detail(self, book_uuid: str) -> None:
        if any(book.uuid == book_uuid for book in self.books):
            self.detail_book_uuid = book_uuid
            self._emit_state()

    def hide_detail(self) -> None:
        if self.detail_book_uuid is None:
            return
        self.detail_book_uuid = None
        self._emit_state()

    def open_book(self, book_uuid: str) -> None:
        if any(book.uuid == book_uuid for book in self.books):
            self.book_open_requested.emit(book_uuid)

    def toggle_favourite(self, book_uuid: str) -> None:
        book = next((book for book in self.books if book.uuid == book_uuid), None)
        if book is not None:
            self.set_favourite((book_uuid,), not book.is_favourite)

    def set_favourite(self, book_uuids: Iterable[str], is_favourite: bool) -> None:
        target_ids = set(book_uuids)
        if not target_ids:
            return
        changed = False
        next_books: list[Book] = []
        for book in self.books:
            if book.uuid in target_ids and book.is_favourite != is_favourite:
                next_books.append(book.with_favourite(is_favourite))
                changed = True
            else:
                next_books.append(book)
        if changed:
            self.books = next_books
            self._emit_state()

    def request_cover_generation_for_loaded_books(self) -> None:
        if self._thumbnail_service is None or self._task_service is None or self._cover_size is None:
            return

        for book in self.books:
            existing = self._thumbnail_service.existing_cover_path(book, self._cover_size)
            if existing is not None:
                self._record_cover(book.uuid, existing)
                continue
            if book.uuid in self._pending_cover_ids or not self._thumbnail_service.can_generate_from(book):
                continue

            self._pending_cover_ids.add(book.uuid)
            self._task_service.submit(
                f"cover-{book.uuid}",
                lambda book=book: self._thumbnail_service.generate_cover(book, self._cover_size),
                on_success=lambda path, book_uuid=book.uuid: self._handle_cover_result(book_uuid, path),
                on_failure=lambda _error, book_uuid=book.uuid: self._pending_cover_ids.discard(book_uuid),
            )

    def request_detail_thumbnails(self, book_uuid: str, size: tuple[int, int]) -> None:
        if (
            self._thumbnail_service is None
            or self._task_service is None
            or self.detail_book_uuid != book_uuid
        ):
            return

        book = next((book for book in self.books if book.uuid == book_uuid), None)
        if book is None or not self._thumbnail_service.can_generate_from(book):
            return

        for page_index in range(max(0, book.page_count)):
            key = (book_uuid, page_index, size)
            cached = self._page_thumbnail_bytes.get(key)
            if cached is not None:
                self.page_thumbnail_ready.emit(book_uuid, page_index, cached)
                continue
            if key in self._pending_page_thumbnail_keys:
                continue

            self._pending_page_thumbnail_keys.add(key)
            self._task_service.submit(
                f"detail-thumbnail-{book_uuid}-{page_index}",
                lambda book=book, page_index=page_index: self._thumbnail_service.generate_page_thumbnail(
                    book,
                    page_index,
                    size,
                ),
                on_success=lambda data, key=key: self._handle_page_thumbnail_result(key, data),
                on_failure=lambda _error, key=key: self._pending_page_thumbnail_keys.discard(key),
            )

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
        if self.detail_book_uuid is not None and self.detail_book_uuid not in visible_ids:
            self.detail_book_uuid = None
        self.state_changed.emit()

    def _record_cover(self, book_uuid: str, path: Path) -> None:
        self._cover_paths[book_uuid] = path
        self.cover_ready.emit(book_uuid, path)

    def _handle_cover_result(self, book_uuid: str, path: Path | None) -> None:
        self._pending_cover_ids.discard(book_uuid)
        if path is None or not any(book.uuid == book_uuid for book in self.books):
            return
        self._record_cover(book_uuid, path)

    def _handle_page_thumbnail_result(
        self,
        key: tuple[str, int, tuple[int, int]],
        data: bytes | None,
    ) -> None:
        self._pending_page_thumbnail_keys.discard(key)
        if data is None:
            return

        book_uuid, page_index, _size = key
        self._page_thumbnail_bytes[key] = data
        if self.detail_book_uuid == book_uuid:
            self.page_thumbnail_ready.emit(book_uuid, page_index, data)


def collection_shelf_key(collection_uuid: str) -> str:
    return f"collection:{collection_uuid}"
