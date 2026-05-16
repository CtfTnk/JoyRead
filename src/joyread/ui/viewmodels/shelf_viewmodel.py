"""Bookshelf ViewModel with mock-data filtering and selection state."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.models.language import Language
from joyread.core.services.cache_service import BoundedByteCache
from joyread.core.services.library_service import LibraryService
from joyread.core.services.task_service import TaskHandle, TaskService
from joyread.core.services.thumbnail_service import DetailThumbnailBatch, ThumbnailService
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.ui.viewmodels.signals import Signal


_DEFAULT_DETAIL_THUMBNAIL_CACHE_MB = 64


logger = logging.getLogger(__name__)


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
        settings: AppSettings | None = None,
        settings_store: SettingsStore | None = None,
        detail_thumbnail_cache_mb: int | None = None,
    ) -> None:
        self.state_changed: Signal[None] = Signal()
        self.selection_changed: Signal[set[str]] = Signal()
        self.book_open_requested: Signal[str] = Signal()
        self.book_open_at_requested: Signal[tuple[str, int]] = Signal()
        self.missing_book_requested: Signal[str] = Signal()
        # cover_ready / page_thumbnail_ready / detail_thumbnail_batch_finished
        # fire from the TaskService worker thread (the thumbnail jobs are
        # submitted to the QThreadPool). Receivers in widgets must marshal
        # back to the Qt UI thread (Qt::AutoConnection is the default for
        # Qt signals; this in-process Signal class is used directly, so the
        # subscriber is responsible).
        self.cover_ready: Signal[tuple[str, Path]] = Signal()
        self.page_thumbnail_ready: Signal[tuple[str, int, bytes]] = Signal()
        self.detail_thumbnail_batch_finished: Signal[tuple[str, int, bool]] = Signal()
        self.books_deleted: Signal[tuple[str, ...]] = Signal()
        self.delete_failed: Signal[str] = Signal()
        self.favourite_failed: Signal[str] = Signal()
        self.book_metadata_failed: Signal[str] = Signal()
        self.collections_changed: Signal[str | None] = Signal()
        self.collection_failed: Signal[str] = Signal()
        self.books_added_to_collection: Signal[tuple[str, ...]] = Signal()
        self.remove_failed: Signal[str] = Signal()

        self._library_service = library_service
        self._thumbnail_service = thumbnail_service
        self._task_service = task_service
        self._cover_size = cover_size
        self._settings_store = settings_store
        self._detail_batch_size = 14
        self.books: list[Book] = []
        self.collections: list[Collection] = []
        self.languages: list[Language] = []
        self.search_query = ""
        self.sort_field = _coerce_sort_field(settings.shelf_sort_field if settings is not None else None)
        self.sort_ascending = bool(settings.shelf_sort_ascending) if settings is not None else False
        self.file_filter = _coerce_file_filter(settings.shelf_file_filter if settings is not None else None)
        self.view_mode = _coerce_view_mode(settings.shelf_view_mode if settings is not None else None)
        self.current_shelf = ShelfKey.ALL.value
        self.selected_book_ids: set[str] = set()
        self.detail_book_uuid: str | None = None
        self.is_loading = False
        self.is_importing = False
        self.import_progress = 0
        self.error_message: str | None = None
        self._cover_paths: dict[str, Path] = {}
        self._pending_cover_ids: set[str] = set()
        self._detail_load_token = 0
        self._detail_next_index = 0
        self._detail_has_more = True
        self._detail_batch_pending = False
        self._detail_batch_handle: TaskHandle[DetailThumbnailBatch] | None = None
        # Detail thumbnails are scoped to a single open detail panel. Owning
        # the byte-budgeted cache here means closing the panel deterministic-
        # ally frees those bytes regardless of LRU pressure on other caches.
        detail_mb = detail_thumbnail_cache_mb
        if detail_mb is None and settings is not None:
            detail_mb = getattr(settings, "detail_thumbnail_cache_mb", None)
        if detail_mb is None:
            detail_mb = _DEFAULT_DETAIL_THUMBNAIL_CACHE_MB
        self._detail_thumbnail_cache: BoundedByteCache[tuple[int, int, int], bytes] = BoundedByteCache(
            max_bytes=max(0, int(detail_mb)) * 1024 * 1024,
        )

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
        if self.current_shelf == ShelfKey.RECENT:
            return sorted(
                books,
                key=lambda book: (book.last_read_at or datetime.min, book.title.lower()),
                reverse=True,
            )
        return sorted(books, key=self._sort_key, reverse=not self.sort_ascending)

    @property
    def can_remove_from_current_shelf(self) -> bool:
        return self.current_shelf == ShelfKey.RECENT.value or self.current_shelf.startswith("collection:")

    def load_books(self) -> None:
        logger.debug("Shelf load_books")
        self.is_loading = True
        self.error_message = None
        self._emit_state()
        try:
            self.books = self._library_service.list_books()
            self.collections = self._library_service.list_collections()
            self.languages = self._library_service.list_languages()
        except Exception as exc:  # pragma: no cover - repository failures are not in mock path.
            logger.warning("Shelf load_books failed: %s", exc, exc_info=True)
            self.error_message = str(exc)
            self.books = []
            self.collections = []
            self.languages = []
        finally:
            self.is_loading = False
            logger.debug(
                "Shelf load_books finished: books=%d collections=%d languages=%d error=%s",
                len(self.books),
                len(self.collections),
                len(self.languages),
                self.error_message,
            )
            self._emit_state()

    def replace_services(
        self,
        library_service: LibraryService,
        thumbnail_service: ThumbnailService | None = None,
    ) -> None:
        self._library_service = library_service
        if thumbnail_service is not None:
            self._thumbnail_service = thumbnail_service
        self._cover_paths.clear()
        self._pending_cover_ids.clear()
        self._set_detail_book_uuid(None)

    def set_current_shelf(self, shelf: str) -> None:
        if shelf == self.current_shelf:
            return
        self.current_shelf = shelf
        self.clear_selection(emit_state=False)
        self._set_detail_book_uuid(None)
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
            self._save_shelf_preferences()
            self._emit_state()

    def set_filter(self, filter_name: str) -> None:
        normalized_filter = FileFilter(filter_name)
        if normalized_filter == self.file_filter:
            return
        self.file_filter = normalized_filter
        self.clear_selection(emit_state=False)
        self._save_shelf_preferences()
        self._emit_state()

    def set_view_mode(self, mode: str) -> None:
        normalized_mode = ViewMode(mode)
        if normalized_mode == self.view_mode:
            return
        self.view_mode = normalized_mode
        self._save_shelf_preferences()
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
        book = self._refresh_book_state(book_uuid)
        if book is None:
            return
        if book.is_missing:
            self.missing_book_requested.emit(book_uuid)
            return
        self._set_detail_book_uuid(book_uuid)
        self._emit_state()

    def hide_detail(self) -> None:
        if self.detail_book_uuid is None:
            return
        self._set_detail_book_uuid(None)
        self._emit_state()

    def open_book(self, book_uuid: str) -> None:
        book = self._refresh_book_state(book_uuid)
        if book is None:
            return
        if book.is_missing:
            self.missing_book_requested.emit(book_uuid)
            return
        self.book_open_requested.emit(book_uuid)

    def open_book_at(self, book_uuid: str, page_index: int) -> None:
        book = self._refresh_book_state(book_uuid)
        if book is None:
            return
        if book.is_missing:
            self.missing_book_requested.emit(book_uuid)
            return
        normalized_index = max(0, page_index)
        self.book_open_at_requested.emit(book_uuid, normalized_index)

    def apply_reader_progress(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        del page_index
        now = datetime.now()
        changed = False
        next_books: list[Book] = []
        normalized_progress = max(0.0, min(100.0, progress_percent)) / 100.0
        for book in self.books:
            if book.uuid == book_uuid:
                next_books.append(
                    replace(
                        book,
                        progress=normalized_progress,
                        last_read_at=now,
                        updated_at=now,
                    )
                )
                changed = True
            else:
                next_books.append(book)
        if changed:
            self.books = next_books
            self._emit_state()

    def toggle_favourite(self, book_uuid: str) -> None:
        book = next((book for book in self.books if book.uuid == book_uuid), None)
        if book is not None:
            self.set_favourite((book_uuid,), not book.is_favourite)

    def set_favourite(self, book_uuids: Iterable[str], is_favourite: bool) -> None:
        target_ids = tuple(dict.fromkeys(book_uuid for book_uuid in book_uuids if book_uuid))
        if not target_ids:
            return

        if self._task_service is None:
            try:
                self._library_service.set_favourites(target_ids, is_favourite)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning(
                    "set_favourites failed books=%s value=%s: %s",
                    target_ids,
                    is_favourite,
                    exc,
                    exc_info=True,
                )
                self.favourite_failed.emit(str(exc))
                return
            self._handle_favourite_success(target_ids, is_favourite)
            return

        self._task_service.submit(
            "set-favourite",
            lambda target_ids=target_ids: self._library_service.set_favourites(target_ids, is_favourite),
            on_success=lambda _result, target_ids=target_ids: self._handle_favourite_success(
                target_ids,
                is_favourite,
            ),
            on_failure=lambda error, target_ids=target_ids: self._emit_favourite_failed(target_ids, error),
        )

    def _emit_favourite_failed(self, target_ids: tuple[str, ...], error: Exception) -> None:
        logger.warning("set_favourites task failed books=%s: %s", target_ids, error)
        self.favourite_failed.emit(str(error))

    def update_book_title(self, book_uuid: str, title: str) -> None:
        normalized_title = _normalize_detail_text(title)
        self.update_book_metadata(book_uuid, title=normalized_title)

    def update_book_author(self, book_uuid: str, author: str) -> None:
        normalized_author = _normalize_detail_text(author)
        self.update_book_metadata(book_uuid, author=normalized_author)

    def update_book_language(self, book_uuid: str, language_tag: str) -> None:
        self.update_book_metadata(book_uuid, language_tag=language_tag)

    def update_book_metadata(
        self,
        book_uuid: str,
        *,
        title: str | None = None,
        author: str | None = None,
        language_tag: str | None = None,
    ) -> None:
        book = next((book for book in self.books if book.uuid == book_uuid), None)
        if book is None:
            return

        next_title = title if title is not None else book.title
        next_author = author if author is not None else book.author
        next_language_tag = language_tag if language_tag is not None else book.language_tag
        next_language_name = book.language_name
        if language_tag is not None:
            next_language = self._language_by_tag(language_tag)
            if next_language is None:
                self.book_metadata_failed.emit(f"Unknown language code: {language_tag}")
                return
            next_language_name = next_language.plain_text
        title_changed = title is not None and title != book.title
        author_changed = author is not None and author != book.author
        language_changed = language_tag is not None and language_tag != book.language_tag
        if not title_changed and not author_changed and not language_changed:
            return

        if self._task_service is None:
            try:
                self._library_service.update_book_metadata(
                    book_uuid,
                    title=title if title_changed else None,
                    author=author if author_changed else None,
                    language_tag=language_tag if language_changed else None,
                )
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                self._handle_book_metadata_failure(exc)
                return
            self._handle_book_metadata_success(
                book_uuid,
                next_title,
                next_author,
                next_language_tag,
                next_language_name,
            )
            return

        self._task_service.submit(
            "update-book-metadata",
            lambda: self._library_service.update_book_metadata(
                book_uuid,
                title=title if title_changed else None,
                author=author if author_changed else None,
                language_tag=language_tag if language_changed else None,
            ),
            on_success=lambda _result: self._handle_book_metadata_success(
                book_uuid,
                next_title,
                next_author,
                next_language_tag,
                next_language_name,
            ),
            on_failure=lambda error: self._handle_book_metadata_failure(error),
        )

    def create_collection(self, name: str) -> None:
        normalized_name = _normalize_collection_name(name)
        if normalized_name is None:
            self.collection_failed.emit("Collection name cannot be empty.")
            return

        if self._task_service is None:
            try:
                collection = self._library_service.create_collection(normalized_name)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning("create_collection failed name=%r: %s", normalized_name, exc)
                self.collection_failed.emit(str(exc))
                return
            self._handle_collection_created(collection)
            return

        self._task_service.submit(
            "create-collection",
            lambda: self._library_service.create_collection(normalized_name),
            on_success=self._handle_collection_created,
            on_failure=lambda error: self._emit_collection_failed("create", error),
        )

    def rename_collection(self, collection_uuid: str, name: str) -> None:
        normalized_name = _normalize_collection_name(name)
        if normalized_name is None:
            self.collection_failed.emit("Collection name cannot be empty.")
            return

        if self._task_service is None:
            try:
                self._library_service.rename_collection(collection_uuid, normalized_name)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning(
                    "rename_collection failed id=%s name=%r: %s",
                    collection_uuid,
                    normalized_name,
                    exc,
                )
                self.collection_failed.emit(str(exc))
                return
            self._handle_collection_changed()
            return

        self._task_service.submit(
            "rename-collection",
            lambda: self._library_service.rename_collection(collection_uuid, normalized_name),
            on_success=lambda _result: self._handle_collection_changed(),
            on_failure=lambda error: self._emit_collection_failed("rename", error),
        )

    def delete_collection(self, collection_uuid: str) -> None:
        collection_key = collection_shelf_key(collection_uuid)
        if self._task_service is None:
            try:
                self._library_service.delete_collection(collection_uuid)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning("delete_collection failed id=%s: %s", collection_uuid, exc)
                self.collection_failed.emit(str(exc))
                return
            self._handle_collection_deleted(collection_key)
            return

        self._task_service.submit(
            "delete-collection",
            lambda: self._library_service.delete_collection(collection_uuid),
            on_success=lambda _result, collection_key=collection_key: self._handle_collection_deleted(collection_key),
            on_failure=lambda error: self._emit_collection_failed("delete", error),
        )

    def add_books_to_collection(self, book_uuids: Iterable[str], collection_uuid: str) -> None:
        target_ids = tuple(dict.fromkeys(book_uuid for book_uuid in book_uuids if book_uuid))
        if not target_ids:
            return

        if self._task_service is None:
            try:
                self._library_service.add_books_to_collection(target_ids, collection_uuid)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning(
                    "add_books_to_collection failed books=%s collection=%s: %s",
                    target_ids,
                    collection_uuid,
                    exc,
                )
                self.collection_failed.emit(str(exc))
                return
            self._handle_books_added_to_collection(target_ids)
            return

        self._task_service.submit(
            "add-books-to-collection",
            lambda: self._library_service.add_books_to_collection(target_ids, collection_uuid),
            on_success=lambda _result, target_ids=target_ids: self._handle_books_added_to_collection(target_ids),
            on_failure=lambda error: self._emit_collection_failed("add-books", error),
        )

    def _emit_collection_failed(self, operation: str, error: Exception) -> None:
        logger.warning("Collection %s task failed: %s", operation, error)
        self.collection_failed.emit(str(error))

    def remove_books_from_current_shelf(self, book_uuids: Iterable[str]) -> None:
        target_ids = tuple(dict.fromkeys(book_uuid for book_uuid in book_uuids if book_uuid))
        if not target_ids or not self.can_remove_from_current_shelf:
            return

        current_shelf = self.current_shelf
        if current_shelf == ShelfKey.RECENT.value:
            def remove_books() -> None:
                self._library_service.remove_books_from_recent(target_ids)
        else:
            collection_uuid = self._collection_uuid_from_shelf(current_shelf)

            def remove_books() -> None:
                self._library_service.remove_books_from_collection(target_ids, collection_uuid)

        if self._task_service is None:
            try:
                remove_books()
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                self._handle_books_removed_from_shelf_failure(exc)
                return
            self._handle_books_removed_from_shelf(target_ids)
            return

        self._task_service.submit(
            "remove-books-from-shelf",
            remove_books,
            on_success=lambda _result, target_ids=target_ids: self._handle_books_removed_from_shelf(target_ids),
            on_failure=self._handle_books_removed_from_shelf_failure,
        )

    def _handle_favourite_success(self, target_ids: tuple[str, ...], is_favourite: bool) -> None:
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

    def _handle_book_metadata_success(
        self,
        book_uuid: str,
        title: str,
        author: str | None,
        language_tag: str | None,
        language_name: str | None,
    ) -> None:
        self.books = [
            replace(
                book,
                title=title,
                author=author,
                language_tag=language_tag,
                language_name=language_name,
                updated_at=datetime.now(),
            )
            if book.uuid == book_uuid
            else book
            for book in self.books
        ]
        self._emit_state()

    def _handle_book_metadata_failure(self, error: Exception) -> None:
        logger.warning("update_book_metadata failed: %s", error)
        self.load_books()
        self.book_metadata_failed.emit(str(error))

    def _handle_collection_created(self, collection: Collection) -> None:
        next_shelf = collection_shelf_key(collection.uuid)
        self.current_shelf = next_shelf
        self.clear_selection(emit_state=False)
        self._set_detail_book_uuid(None)
        self.load_books()
        self._emit_state()
        self.collections_changed.emit(next_shelf)

    def _handle_collection_changed(self) -> None:
        self.load_books()
        self.collections_changed.emit(self.current_shelf)

    def _handle_collection_deleted(self, collection_key: str) -> None:
        if self.current_shelf == collection_key:
            self.current_shelf = ShelfKey.ALL.value
            self.clear_selection(emit_state=False)
            self._set_detail_book_uuid(None)
        self.load_books()
        self.collections_changed.emit(self.current_shelf)

    def _handle_books_added_to_collection(self, target_ids: tuple[str, ...]) -> None:
        self.load_books()
        self.books_added_to_collection.emit(target_ids)

    def _handle_books_removed_from_shelf(self, _target_ids: tuple[str, ...]) -> None:
        self.load_books()

    def _handle_books_removed_from_shelf_failure(self, error: Exception) -> None:
        logger.warning("remove_books_from_current_shelf failed: %s", error)
        self.load_books()
        self.remove_failed.emit(str(error))

    def delete_books(self, book_uuids: Iterable[str]) -> None:
        target_ids = tuple(dict.fromkeys(book_uuid for book_uuid in book_uuids if book_uuid))
        if not target_ids:
            return

        logger.info("delete_books request books=%s", target_ids)
        if self._task_service is None:
            try:
                self._library_service.delete_books(target_ids)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning("delete_books failed books=%s: %s", target_ids, exc, exc_info=True)
                self.delete_failed.emit(str(exc))
                return
            self._handle_delete_success(target_ids)
            return

        self._task_service.submit(
            "delete-books",
            lambda target_ids=target_ids: self._library_service.delete_books(target_ids),
            on_success=lambda _result, target_ids=target_ids: self._handle_delete_success(target_ids),
            on_failure=lambda error, target_ids=target_ids: self._handle_delete_failure(error, target_ids),
        )

    def request_cover_generation_for_loaded_books(self) -> None:
        self.request_covers_for_books(book.uuid for book in self.books)

    def request_covers_for_books(self, book_uuids: Iterable[str]) -> None:
        if self._thumbnail_service is None or self._task_service is None or self._cover_size is None:
            return

        target_ids = set(book_uuids)
        books_by_uuid = {book.uuid: book for book in self.books}
        for book_uuid in target_ids:
            book = books_by_uuid.get(book_uuid)
            if book is None:
                continue
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
        self.request_next_detail_thumbnail_batch(book_uuid, size)

    def request_next_detail_thumbnail_batch(self, book_uuid: str, size: tuple[int, int]) -> None:
        if (
            self._thumbnail_service is None
            or self._task_service is None
            or self.detail_book_uuid != book_uuid
            or self._detail_batch_pending
            or not self._detail_has_more
        ):
            return

        book = next((book for book in self.books if book.uuid == book_uuid), None)
        if book is None or not self._thumbnail_service.can_generate_from(book):
            self._detail_has_more = False
            return

        token = self._detail_load_token
        start_index = self._detail_next_index
        self._detail_batch_pending = True
        detail_cache = self._detail_thumbnail_cache
        self._detail_batch_handle = self._task_service.submit(
            f"detail-thumbnail-batch-{book_uuid}-{start_index}",
            lambda book=book, start_index=start_index: self._thumbnail_service.generate_detail_thumbnail_batch(
                book,
                start_index=start_index,
                batch_size=self._detail_batch_size,
                size=size,
                detail_cache=detail_cache,
            ),
            on_success=lambda batch, token=token: self._handle_detail_thumbnail_batch_result(token, batch),
            on_failure=lambda _error, token=token: self._handle_detail_thumbnail_batch_failure(token),
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

    def _book_by_uuid(self, book_uuid: str) -> Book | None:
        return next((book for book in self.books if book.uuid == book_uuid), None)

    def _refresh_book_state(self, book_uuid: str) -> Book | None:
        # Query the repository on demand so actions reflect missing files
        # removed while the app is running.
        try:
            refreshed = self._library_service.get_book(book_uuid)
        except Exception as exc:  # pragma: no cover - repository-specific failure path.
            logger.warning("refresh_book_state failed book=%s: %s", book_uuid, exc, exc_info=True)
            return self._book_by_uuid(book_uuid)
        if refreshed is None:
            return None
        current = self._book_by_uuid(book_uuid)
        if current is None:
            return refreshed
        if current == refreshed:
            return refreshed
        self.books = [refreshed if book.uuid == book_uuid else book for book in self.books]
        self._emit_state()
        return refreshed

    def _emit_state(self) -> None:
        visible_ids = {book.uuid for book in self.visible_books}
        removed = self.selected_book_ids - visible_ids
        if removed:
            self.selected_book_ids -= removed
            self.selection_changed.emit(set(self.selected_book_ids))
        if self.detail_book_uuid is not None and self.detail_book_uuid not in visible_ids:
            self._set_detail_book_uuid(None)
        self.state_changed.emit()

    def _save_shelf_preferences(self) -> None:
        if self._settings_store is None:
            return
        self._settings_store.update(
            shelf_sort_field=self.sort_field.value,
            shelf_sort_ascending=self.sort_ascending,
            shelf_file_filter=self.file_filter.value,
            shelf_view_mode=self.view_mode.value,
        )

    def _handle_delete_success(self, target_ids: tuple[str, ...]) -> None:
        target_set = set(target_ids)
        self.selected_book_ids -= target_set
        if self.detail_book_uuid in target_set:
            self._set_detail_book_uuid(None)
        self._cover_paths = {
            book_uuid: path for book_uuid, path in self._cover_paths.items() if book_uuid not in target_set
        }
        self._pending_cover_ids -= target_set
        self.load_books()
        self.selection_changed.emit(set(self.selected_book_ids))
        self.books_deleted.emit(target_ids)

    def _handle_delete_failure(self, error: Exception, target_ids: tuple[str, ...]) -> None:
        logger.warning("delete_books task failed books=%s: %s", target_ids, error)
        target_set = set(target_ids)
        self.selected_book_ids -= target_set
        if self.detail_book_uuid in target_set:
            self._set_detail_book_uuid(None)
        self.load_books()
        self.selection_changed.emit(set(self.selected_book_ids))
        self.delete_failed.emit(str(error))

    def _record_cover(self, book_uuid: str, path: Path) -> None:
        if self._cover_paths.get(book_uuid) == path:
            return
        self._cover_paths[book_uuid] = path
        self.cover_ready.emit(book_uuid, path)

    def _handle_cover_result(self, book_uuid: str, path: Path | None) -> None:
        self._pending_cover_ids.discard(book_uuid)
        if path is None or not any(book.uuid == book_uuid for book in self.books):
            return
        self._record_cover(book_uuid, path)

    def _handle_detail_thumbnail_batch_result(self, token: int, batch: DetailThumbnailBatch) -> None:
        if token != self._detail_load_token or self.detail_book_uuid != batch.book_uuid:
            return

        self._detail_batch_pending = False
        self._detail_batch_handle = None
        self._detail_next_index = batch.next_index
        self._detail_has_more = batch.has_more
        for item in batch.items:
            self.page_thumbnail_ready.emit(batch.book_uuid, item.page_index, item.image_bytes)
        self.detail_thumbnail_batch_finished.emit(batch.book_uuid, batch.next_index, batch.has_more)

    def _handle_detail_thumbnail_batch_failure(self, token: int) -> None:
        if token != self._detail_load_token:
            return
        self._detail_batch_pending = False
        self._detail_batch_handle = None
        self._detail_has_more = False

    def _set_detail_book_uuid(self, book_uuid: str | None) -> None:
        if self.detail_book_uuid == book_uuid:
            return
        self._cancel_detail_thumbnail_batch()
        # Closing the detail panel (or switching to a different book) frees
        # the cached PNGs immediately. This is the deterministic counterpart
        # to the byte budget — the budget caps how high memory can go while
        # detail is open, this call guarantees zero bytes when it is closed.
        self._detail_thumbnail_cache.clear()
        self.detail_book_uuid = book_uuid
        self._detail_load_token += 1
        self._detail_next_index = 0
        self._detail_has_more = book_uuid is not None
        self._detail_batch_pending = False

    def resize_detail_thumbnail_cache(self, max_bytes: int) -> None:
        """Live-resize the detail thumbnail budget from the settings panel."""

        self._detail_thumbnail_cache.resize(max(0, int(max_bytes)))

    def _cancel_detail_thumbnail_batch(self) -> None:
        if self._detail_batch_handle is not None:
            self._detail_batch_handle.cancel()
        self._detail_batch_handle = None

    def _language_by_tag(self, language_tag: str) -> Language | None:
        return next((language for language in self.languages if language.iso_code == language_tag), None)


def collection_shelf_key(collection_uuid: str) -> str:
    return f"collection:{collection_uuid}"


def _normalize_collection_name(name: str) -> str | None:
    normalized = name.strip()
    return normalized or None


def _normalize_detail_text(value: str) -> str:
    return value.strip() or "None"


def _coerce_sort_field(value: str | None) -> SortField:
    try:
        return SortField(value)
    except ValueError:
        return SortField.ADD_TIME


def _coerce_file_filter(value: str | None) -> FileFilter:
    try:
        return FileFilter(value)
    except ValueError:
        return FileFilter.ALL


def _coerce_view_mode(value: str | None) -> ViewMode:
    try:
        return ViewMode(value)
    except ValueError:
        return ViewMode.GRID
