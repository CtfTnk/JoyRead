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
from joyread.core.models.tag import Tag
from joyread.core.search import (
    BookSearchDocument,
    BookSearchQuery,
    build_book_search_document,
    matches_book_search,
    parse_book_search_query,
)
from joyread.core.services.library_service import LibraryService
from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.core.services.tag_service import TagService
from joyread.app.tasking import TaskExecutor, TaskHandle, TaskPriority
from joyread.core.services.thumbnail_service import ThumbnailService, ThumbnailSourceHandle
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.viewmodels.signals import Signal
from joyread.app.thumbnail_stream import ThumbnailStreamController, ThumbnailStreamItem


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
    HIDDEN = "hidden"


class ShelfViewModel:
    """ViewModel for the bookshelf grid/list and detail panel.

    Owns the shelf's UI state — the current section (All / Recent /
    Favourites / Hidden / a collection / a tag filter), the search query,
    the active sort and file filter, the selection set, and the in-memory
    list of :class:`Book` rows.

    **Threading contract.** Most callers (View slots, ``set_filter``,
    ``open_book``) run on the Qt UI thread and update state synchronously,
    then fire ``state_changed`` so the view repaints. Three signals,
    however, are emitted from inside a :class:`TaskService` worker thread:

    - ``cover_ready``
    - ``page_thumbnail_ready``
    - ``detail_thumbnail_source_ready``

    These are produced by thumbnail jobs running off-UI. Receivers that
    touch widgets (paint, layout) MUST re-marshal back onto the Qt UI
    thread, typically via ``QTimer.singleShot(0, ...)`` or by routing
    through a Qt signal. Treat anything not on this short list as UI-thread.

    The token ``_detail_load_token`` exists to defeat race conditions when
    the user opens detail panel for book A, then quickly switches to book B
    before A's thumbnail source or stream item finishes. Each detail-panel
    open bumps the token; late results compare against the captured token and
    are dropped when stale.
    """

    def __init__(
        self,
        library_service: LibraryService,
        thumbnail_service: ThumbnailService | None = None,
        task_service: TaskExecutor | None = None,
        cover_size: tuple[int, int] | None = None,
        settings: AppSettings | None = None,
        settings_store: SettingsStore | None = None,
        tag_service: TagService | None = None,
        archive_warmup_coordinator: ArchiveWarmupCoordinator | None = None,
    ) -> None:
        self.state_changed: Signal[None] = Signal()
        self.selection_changed: Signal[set[str]] = Signal()
        self.book_open_requested: Signal[str] = Signal()
        self.book_open_at_requested: Signal[tuple[str, int]] = Signal()
        self.missing_book_requested: Signal[str] = Signal()
        self.unavailable_book_requested: Signal[str] = Signal()
        # cover_ready / page_thumbnail_ready / detail_thumbnail_source_ready
        # fire from the TaskService worker thread (the thumbnail jobs are
        # submitted to the QThreadPool). Receivers in widgets must marshal
        # back to the Qt UI thread (Qt::AutoConnection is the default for
        # Qt signals; this in-process Signal class is used directly, so the
        # subscriber is responsible).
        self.cover_ready: Signal[tuple[str, Path]] = Signal()
        self.page_thumbnail_ready: Signal[tuple[str, int, bytes]] = Signal()
        self.detail_thumbnail_source_ready: Signal[tuple[str, int]] = Signal()
        self.books_deleted: Signal[tuple[str, ...]] = Signal()
        self.delete_failed: Signal[str] = Signal()
        self.favourite_failed: Signal[str] = Signal()
        self.book_metadata_failed: Signal[str] = Signal()
        self.book_cover_updated: Signal[tuple[str, Path]] = Signal()
        self.book_cover_failed: Signal[str] = Signal()
        self.collections_changed: Signal[str | None] = Signal()
        self.collection_failed: Signal[str] = Signal()
        self.books_added_to_collection: Signal[tuple[str, ...]] = Signal()
        self.remove_failed: Signal[str] = Signal()
        self.book_tags_failed: Signal[str] = Signal()

        self._library_service = library_service
        self._thumbnail_service = thumbnail_service
        self._task_service = task_service
        self._tag_service = tag_service
        self._archive_warmup_coordinator = archive_warmup_coordinator
        self._detail_warmup_client_id = f"detail-thumbnail:{id(self)}"
        self._cover_size = cover_size
        self._settings_store = settings_store
        self.books: list[Book] = []
        self.collections: list[Collection] = []
        self.languages: list[Language] = []
        self._search_documents_by_book_id: dict[str, BookSearchDocument] = {}
        self.available_tags: list[Tag] = []
        self._book_tag_ids_by_book: dict[str, tuple[str, ...]] = {}
        # Guards against an older async tag read publishing after a newer one.
        self._tag_refresh_token = 0
        self._tag_filter_ids: tuple[str, ...] = ()
        self.search_query = ""
        self.sort_field = _coerce_sort_field(settings.shelf_sort_field if settings is not None else None)
        self.sort_ascending = bool(settings.shelf_sort_ascending) if settings is not None else False
        self.file_filter = _coerce_file_filter(settings.shelf_file_filter if settings is not None else None)
        self.view_mode = _coerce_view_mode(settings.shelf_view_mode if settings is not None else None)
        self.current_shelf = ShelfKey.ALL.value
        self.show_hidden_collection = bool(settings.show_hidden_collection) if settings is not None else False
        self.hidden_space_initialized = bool(
            settings is not None and settings.hidden_space_password_hash is not None
        )
        self.selected_book_ids: set[str] = set()
        self.detail_book_uuid: str | None = None
        self.is_loading = False
        self.is_importing = False
        self.import_progress = 0
        self.error_message: str | None = None
        self._cover_paths: dict[str, Path] = {}
        self._pending_cover_ids: set[str] = set()
        self._detail_load_token = 0
        self._detail_source_handle: ThumbnailSourceHandle | None = None
        self._detail_source_task: TaskHandle[ThumbnailSourceHandle | None] | None = None
        self._detail_thumbnail_size = (1, 1)
        self._detail_pending_interest: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
        self._detail_stream: ThumbnailStreamController | None = None
        self._rebuild_detail_stream()

    @property
    def cover_paths(self) -> dict[str, Path]:
        return dict(self._cover_paths)

    @property
    def tag_filter_ids(self) -> tuple[str, ...]:
        return self._tag_filter_ids

    @property
    def tag_filter_active(self) -> bool:
        return bool(self._tag_filter_ids)

    @property
    def page_title(self) -> str:
        if self.current_shelf == ShelfKey.ALL:
            return "All"
        if self.current_shelf == ShelfKey.RECENT:
            return "Recent"
        if self.current_shelf == ShelfKey.FAVOURITES:
            return "Favourites"
        if self.current_shelf == ShelfKey.HIDDEN:
            return "Hidden"
        collection_uuid = self._collection_uuid_from_shelf(self.current_shelf)
        for collection in self.collections:
            if collection.uuid == collection_uuid:
                return collection.name
        return "Collection"

    @property
    def visible_books(self) -> list[Book]:
        books = [book for book in self.books if self._book_in_current_shelf(book)]
        search_query = parse_book_search_query(self.search_query)
        if not search_query.is_empty:
            books = [book for book in books if self._book_matches_search(book, search_query)]
        books = [book for book in books if self._book_matches_filter(book)]
        books = [book for book in books if self._book_matches_tag_filter(book)]
        if self.current_shelf == ShelfKey.RECENT:
            return sorted(
                books,
                key=lambda book: (book.last_read_at or datetime.min, book.title.lower()),
                reverse=True,
            )
        return sorted(books, key=self._sort_key, reverse=not self.sort_ascending)

    @property
    def visible_collections(self) -> list[Collection]:
        # Hidable collections are part of the Hidden Space surface. They are
        # filtered out of the sidebar whenever the "Show Collections" toggle
        # is off so a hidable collection can't leak the existence of hidden
        # content while the feature is dormant.
        if self.show_hidden_collection:
            return list(self.collections)
        return [collection for collection in self.collections if not collection.is_hidable]

    @property
    def can_remove_from_current_shelf(self) -> bool:
        return self.current_shelf == ShelfKey.RECENT.value or self.current_shelf.startswith("collection:")

    def load_books(self) -> None:
        """Refresh every shelf-backing list from the library service.

        Reloads books, collections, languages, search documents, and the
        book → tag-ids index in one shot. Called on app start, after an
        import, after a tag rename/delete, and after any operation that
        changes the underlying database. The view flashes through
        ``is_loading=True`` so the spinner state shows for slow refreshes.
        """

        logger.debug("Shelf load_books")
        self.is_loading = True
        self.error_message = None
        self._emit_state()
        try:
            self.books = self._library_service.list_books()
            self.collections = self._library_service.list_collections()
            self.languages = self._library_service.list_languages()
            self._refresh_search_documents()
            self._refresh_book_tag_index()
        except Exception as exc:  # pragma: no cover - repository failures are not in mock path.
            logger.warning("Shelf load_books failed: %s", exc, exc_info=True)
            self.error_message = str(exc)
            self.books = []
            self.collections = []
            self.languages = []
            self._search_documents_by_book_id = {}
            self.available_tags = []
            self._book_tag_ids_by_book = {}
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
        tag_service: TagService | None = None,
    ) -> None:
        self._library_service = library_service
        if thumbnail_service is not None:
            self._thumbnail_service = thumbnail_service
        if tag_service is not None:
            self._tag_service = tag_service
        self._cover_paths.clear()
        self._pending_cover_ids.clear()
        self._refresh_book_tag_index()
        self._set_detail_book_uuid(None)
        self._rebuild_detail_stream()

    def set_show_hidden_collection(self, enabled: bool) -> None:
        # Settings layer is the single source of truth; the VM mirrors it
        # so the filtering rules don't need to touch SettingsStore on every
        # render. Caller is responsible for persistence (HiddenSpaceService
        # handles that as part of the password-protected toggle flow).
        normalized = bool(enabled)
        if normalized == self.show_hidden_collection:
            return
        self.show_hidden_collection = normalized
        if not normalized and (
            self.current_shelf == ShelfKey.HIDDEN.value
            or self._is_hidable_collection_shelf(self.current_shelf)
        ):
            # Switching off the toggle while sitting on a now-invisible
            # shelf would leave the view stuck on an empty page; redirect
            # to All so the user sees something coherent.
            self.current_shelf = ShelfKey.ALL.value
            self.clear_selection(emit_state=False)
            self._set_detail_book_uuid(None)
        self._emit_state()

    def set_hidden_space_initialized(self, initialized: bool) -> None:
        self.hidden_space_initialized = bool(initialized)
        self._emit_state()

    def set_current_shelf(self, shelf: str) -> None:
        if shelf == self.current_shelf:
            return
        logger.debug("Shelf current_shelf changed %s -> %s", self.current_shelf, shelf)
        self.current_shelf = shelf
        self.clear_selection(emit_state=False)
        self._set_detail_book_uuid(None)
        self._emit_state()

    def set_search_query(self, query: str) -> None:
        if query == self.search_query:
            return
        logger.debug("Shelf search query changed length=%d", len(query))
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
            logger.debug("Shelf sort changed field=%s ascending=%s", self.sort_field.value, self.sort_ascending)
            self._save_shelf_preferences()
            self._emit_state()

    def set_filter(self, filter_name: str) -> None:
        normalized_filter = FileFilter(filter_name)
        if normalized_filter == self.file_filter:
            return
        self.file_filter = normalized_filter
        logger.debug("Shelf file filter changed filter=%s", self.file_filter.value)
        self.clear_selection(emit_state=False)
        self._save_shelf_preferences()
        self._emit_state()

    def set_tag_filter_ids(self, tag_ids: Iterable[str]) -> None:
        normalized_ids = tuple(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id))
        if normalized_ids == self._tag_filter_ids:
            return
        self._tag_filter_ids = normalized_ids
        logger.debug("Shelf tag filter changed count=%d", len(normalized_ids))
        self.clear_selection(emit_state=False)
        self._refresh_book_tag_index()
        self._emit_state()

    def clear_tag_filter(self) -> None:
        self.set_tag_filter_ids(())

    def tag_ids_for_book(self, book_uuid: str) -> tuple[str, ...]:
        return self._book_tag_ids_by_book.get(book_uuid, ())

    def tags_for_book(self, book_uuid: str) -> tuple[Tag, ...]:
        assigned_ids = set(self.tag_ids_for_book(book_uuid))
        return tuple(tag for tag in self.available_tags if tag.tag_id in assigned_ids)

    def set_book_tag_ids(self, book_uuid: str, tag_ids: Iterable[str]) -> None:
        if self._tag_service is None:
            self.book_tags_failed.emit(t("dialog.tag_service_unavailable"))
            return
        book = self._book_by_uuid(book_uuid)
        if book is None:
            self.book_tags_failed.emit("The selected book is no longer available.")
            return
        normalized_ids = tuple(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id))
        if normalized_ids == self.tag_ids_for_book(book_uuid):
            return
        if self._task_service is None:
            try:
                self._tag_service.set_book_tag_ids(book_uuid, normalized_ids)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                self._handle_book_tags_failure(book_uuid, exc)
                return
            self._handle_book_tags_success(book_uuid)
            return

        self._task_service.submit(
            "set-book-tags",
            lambda book_uuid=book_uuid, tag_ids=normalized_ids: self._tag_service.set_book_tag_ids(
                book_uuid,
                tag_ids,
            ),
            on_success=lambda _result, book_uuid=book_uuid: self._handle_book_tags_success(book_uuid),
            on_failure=lambda error, book_uuid=book_uuid: self._handle_book_tags_failure(book_uuid, error),
        )

    def set_view_mode(self, mode: str) -> None:
        normalized_mode = ViewMode(mode)
        if normalized_mode == self.view_mode:
            return
        self.view_mode = normalized_mode
        logger.debug("Shelf view mode changed mode=%s", self.view_mode.value)
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
        if not book.is_available:
            self._emit_unavailable(book_uuid, "show_detail", book)
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
        if not book.is_available:
            self._emit_unavailable(book_uuid, "open_book", book)
            return
        self.book_open_requested.emit(book_uuid)

    def open_book_at(self, book_uuid: str, page_index: int) -> None:
        book = self._refresh_book_state(book_uuid)
        if book is None:
            return
        if not book.is_available:
            self._emit_unavailable(book_uuid, "open_book_at", book)
            return
        normalized_index = max(0, page_index)
        self.book_open_at_requested.emit(book_uuid, normalized_index)

    def apply_reader_progress(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        """Optimistically reflect reader progress on the shelf row.

        Called when the reader emits a progress update. Updates the in-memory
        ``Book`` row's ``progress`` and ``last_read_at`` so the shelf can
        repaint immediately. The actual database write is performed
        separately by the reader through ``LibraryService.set_progress``;
        if the disk write fails, the next ``load_books`` will overwrite the
        optimistic value. ``page_index`` is accepted for symmetry with the
        reader signal but is not stored at the shelf level.
        """

        del page_index
        now = datetime.now()
        changed = False
        changed_books: list[Book] = []
        next_books: list[Book] = []
        normalized_progress = max(0.0, min(100.0, progress_percent)) / 100.0
        for book in self.books:
            if book.uuid == book_uuid:
                updated = replace(
                    book,
                    progress=normalized_progress,
                    last_read_at=now,
                    updated_at=now,
                )
                next_books.append(updated)
                changed_books.append(updated)
                changed = True
            else:
                next_books.append(book)
        if changed:
            self.books = next_books
            self._record_search_documents(changed_books)
            self._emit_state()

    def toggle_favourite(self, book_uuid: str) -> None:
        book = next((book for book in self.books if book.uuid == book_uuid), None)
        if book is not None:
            self.set_favourite((book_uuid,), not book.is_favourite)

    def set_favourite(self, book_uuids: Iterable[str], is_favourite: bool) -> None:
        target_ids = tuple(dict.fromkeys(book_uuid for book_uuid in book_uuids if book_uuid))
        if not target_ids:
            return

        # Hidden books are intentionally not allowed in Favourites — the
        # cascade in ``hide_book`` clears the flag, so re-favouriting here
        # would resurrect a stale row that pops back into Favourites the
        # moment the book is unhidden. Block the action and surface the
        # rule so the user understands why the toggle didn't take.
        if is_favourite:
            blocked = [
                book.uuid for book in self.books if book.uuid in target_ids and book.is_hidden
            ]
            if blocked:
                logger.warning(
                    "set_favourite blocked: hidden books cannot be favourited books=%s",
                    blocked,
                )
                self.favourite_failed.emit(
                    t("dialog.hidden_favourite_blocked")
                )
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
                self.book_metadata_failed.emit(t("dialog.unknown_language_code", code=language_tag))
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

    def set_book_cover_path(self, book_uuid: str, cover_path: Path | str) -> None:
        path = Path(cover_path)
        book = next((book for book in self.books if book.uuid == book_uuid), None)
        if book is None:
            self.book_cover_failed.emit(t("dialog.book_no_longer_available"))
            return

        if self._task_service is None:
            try:
                self._library_service.set_book_cover_path(book_uuid, str(path))
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                self._handle_book_cover_failure(exc)
                return
            self._handle_book_cover_success(book_uuid, path)
            return

        self._task_service.submit(
            "set-book-cover",
            lambda: self._library_service.set_book_cover_path(book_uuid, str(path)),
            on_success=lambda _result, book_uuid=book_uuid, path=path: self._handle_book_cover_success(
                book_uuid,
                path,
            ),
            on_failure=lambda error: self._handle_book_cover_failure(error),
        )

    def create_collection(self, name: str) -> None:
        normalized_name = _normalize_collection_name(name)
        if normalized_name is None:
            self.collection_failed.emit(t("dialog.collection_name_required"))
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
            self.collection_failed.emit(t("dialog.collection_name_required"))
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

        # Normal collections must never contain hidden books — the
        # visibility filter would silently swallow them after the insert,
        # which looks like "nothing happened". Surface the constraint up
        # front so the user can either unhide first or make the target
        # collection hidable.
        target_collection = next(
            (collection for collection in self.collections if collection.uuid == collection_uuid),
            None,
        )
        if target_collection is not None and not target_collection.is_hidable:
            hidden_targets = [
                book.uuid for book in self.books if book.uuid in target_ids and book.is_hidden
            ]
            if hidden_targets:
                logger.warning(
                    "add_books_to_collection blocked: hidden books cannot join a normal collection "
                    "books=%s collection=%s",
                    hidden_targets,
                    collection_uuid,
                )
                self.collection_failed.emit(
                    t("dialog.hidden_normal_collection_blocked")
                )
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

    def hide_books(self, book_uuids: Iterable[str]) -> None:
        self._set_books_hidden(book_uuids, hidden=True)

    def unhide_books(self, book_uuids: Iterable[str]) -> None:
        self._set_books_hidden(book_uuids, hidden=False)

    def _set_books_hidden(self, book_uuids: Iterable[str], *, hidden: bool) -> None:
        target_ids = tuple(dict.fromkeys(book_uuid for book_uuid in book_uuids if book_uuid))
        if not target_ids:
            return

        if self._task_service is None:
            try:
                self._library_service.set_books_hidden(target_ids, hidden)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning(
                    "set_books_hidden failed books=%s value=%s: %s",
                    target_ids,
                    hidden,
                    exc,
                    exc_info=True,
                )
                return
            self._handle_hidden_change()
            return

        self._task_service.submit(
            "set-books-hidden",
            lambda target_ids=target_ids: self._library_service.set_books_hidden(target_ids, hidden),
            on_success=lambda _result: self._handle_hidden_change(),
            on_failure=lambda error: logger.warning("set_books_hidden task failed: %s", error),
        )

    def set_collection_hidable(self, collection_uuid: str, hidable: bool) -> None:
        if self._task_service is None:
            try:
                self._library_service.set_collection_hidable(collection_uuid, hidable)
            except Exception as exc:  # pragma: no cover - repository-specific failure path.
                logger.warning(
                    "set_collection_hidable failed collection=%s value=%s: %s",
                    collection_uuid,
                    hidable,
                    exc,
                    exc_info=True,
                )
                self.collection_failed.emit(str(exc))
                return
            self._handle_hidden_change()
            return

        self._task_service.submit(
            "set-collection-hidable",
            lambda: self._library_service.set_collection_hidable(collection_uuid, hidable),
            on_success=lambda _result: self._handle_hidden_change(),
            on_failure=lambda error: self._emit_collection_failed("set-hidable", error),
        )

    def _handle_hidden_change(self) -> None:
        # Hiding/unhiding mutates several derived columns (is_favourite,
        # recent_books, collection memberships) so the simplest correct
        # refresh is a full reload — matches the favourites/collection
        # mutation pattern above.
        self.load_books()
        self.collections_changed.emit(self.current_shelf)

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
        changed_books: list[Book] = []
        next_books: list[Book] = []
        for book in self.books:
            if book.uuid in target_ids and book.is_favourite != is_favourite:
                updated = book.with_favourite(is_favourite)
                next_books.append(updated)
                changed_books.append(updated)
                changed = True
            else:
                next_books.append(book)
        if changed:
            self.books = next_books
            self._record_search_documents(changed_books)
            self._emit_state()

    def _handle_book_metadata_success(
        self,
        book_uuid: str,
        title: str,
        author: str | None,
        language_tag: str | None,
        language_name: str | None,
    ) -> None:
        updated_book: Book | None = None
        next_books: list[Book] = []
        for book in self.books:
            if book.uuid == book_uuid:
                updated_book = replace(
                    book,
                    title=title,
                    author=author,
                    language_tag=language_tag,
                    language_name=language_name,
                    updated_at=datetime.now(),
                )
                next_books.append(updated_book)
            else:
                next_books.append(book)
        self.books = next_books
        if updated_book is not None:
            self._record_search_documents((updated_book,))
        self._emit_state()

    def _handle_book_metadata_failure(self, error: Exception) -> None:
        logger.warning("update_book_metadata failed: %s", error)
        self.load_books()
        self.book_metadata_failed.emit(str(error))

    def _handle_book_cover_success(self, book_uuid: str, path: Path) -> None:
        path_text = str(path)
        updated_book: Book | None = None
        next_books: list[Book] = []
        for book in self.books:
            if book.uuid == book_uuid:
                updated_book = replace(book, cover_thumbnail_path=path_text, updated_at=datetime.now())
                next_books.append(updated_book)
            else:
                next_books.append(book)
        self.books = next_books
        if updated_book is not None:
            self._record_search_documents((updated_book,))
        self._record_cover(book_uuid, path, force=True)
        self._emit_state()
        self.book_cover_updated.emit(book_uuid, path)

    def _handle_book_cover_failure(self, error: Exception) -> None:
        # Reconstructed from the error object rather than exc_info=True: this
        # handler runs both from a synchronous except block (a live exception)
        # and from a queued TaskService on_failure callback (no exception
        # active on this thread, where exc_info=True would silently log
        # nothing). error.__traceback__ survives either path.
        logger.warning(
            "set_book_cover_path failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self.load_books()
        self.book_cover_failed.emit(str(error))

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
                # Same treatment as the worker path: refresh, keep what still
                # exists, report. Emitting straight through would leave the
                # shelf showing books a partial delete already removed.
                self._handle_delete_failure(exc, target_ids)
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
            kwargs = {
                "on_success": lambda path, book_uuid=book.uuid: self._handle_cover_result(book_uuid, path),
                "on_failure": lambda _error, book_uuid=book.uuid: self._pending_cover_ids.discard(book_uuid),
            }
            try:
                self._task_service.submit(
                    f"cover-{book.uuid}",
                    lambda book=book: self._thumbnail_service.generate_cover(book, self._cover_size),
                    priority=TaskPriority.LOW,
                    **kwargs,
                )
            except TypeError:
                self._task_service.submit(
                    f"cover-{book.uuid}",
                    lambda book=book: self._thumbnail_service.generate_cover(book, self._cover_size),
                    **kwargs,
                )

    def prepare_detail_thumbnail_source(self, book_uuid: str, size: tuple[int, int]) -> None:
        if (
            self._thumbnail_service is None
            or self._task_service is None
            or self.detail_book_uuid != book_uuid
        ):
            return
        normalized_size = (max(1, int(size[0])), max(1, int(size[1])))
        if self._detail_source_handle is not None and self._detail_thumbnail_size == normalized_size:
            return
        if self._detail_source_task is not None:
            return
        book = self._book_by_uuid(book_uuid)
        open_source = getattr(self._thumbnail_service, "open_thumbnail_source", None)
        if book is None or not callable(open_source) or not self._thumbnail_service.can_generate_from(book):
            self.detail_thumbnail_source_ready.emit(book_uuid, 0)
            return
        token = self._detail_load_token
        self._detail_thumbnail_size = normalized_size

        def success(source: ThumbnailSourceHandle | None) -> None:
            self._handle_detail_source_result(token, book_uuid, source, normalized_size)

        try:
            self._detail_source_task = self._task_service.submit(
                f"detail-thumbnail-source-{book_uuid}",
                lambda book=book: open_source(book),
                on_success=success,
                on_failure=lambda error, token=token: self._handle_detail_source_failure(token, error),
                on_discard=lambda source: source.close() if source is not None else None,
                priority=TaskPriority.HIGH,
            )
        except TypeError:
            self._detail_source_task = self._task_service.submit(
                f"detail-thumbnail-source-{book_uuid}",
                lambda book=book: open_source(book),
                on_success=success,
                on_failure=lambda error, token=token: self._handle_detail_source_failure(token, error),
            )

    def set_detail_thumbnail_interest(
        self,
        book_uuid: str,
        visible_indices: Iterable[int],
        prefetch_indices: Iterable[int],
        size: tuple[int, int],
    ) -> None:
        if self.detail_book_uuid != book_uuid:
            return
        visible = tuple(dict.fromkeys(int(index) for index in visible_indices))
        prefetch = tuple(dict.fromkeys(int(index) for index in prefetch_indices))
        self._detail_pending_interest = (visible, prefetch)
        normalized_size = (max(1, int(size[0])), max(1, int(size[1])))
        if self._detail_source_handle is None or self._detail_thumbnail_size != normalized_size:
            self.prepare_detail_thumbnail_source(book_uuid, normalized_size)
            return
        if self._detail_stream is not None:
            self._detail_stream.set_interest(visible, prefetch)
        self._ensure_detail_warmup()

    def release_detail_thumbnail_interest(self, book_uuid: str) -> None:
        if self.detail_book_uuid != book_uuid:
            return
        self._detail_pending_interest = ((), ())
        if self._detail_stream is not None:
            self._detail_stream.release_interest()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._detail_warmup_client_id)

    def refresh_detail_thumbnail_interest(self, book_uuid: str) -> None:
        if self.detail_book_uuid == book_uuid and self._detail_stream is not None:
            self._detail_stream.refresh()

    def invalidate_detail_thumbnail_source(self) -> None:
        """Reopen an active Detail source after archive policy changes."""

        book_uuid = self.detail_book_uuid
        if book_uuid is None:
            return
        if self._detail_source_task is not None:
            self._detail_source_task.cancel()
        self._detail_source_task = None
        if self._detail_source_handle is not None:
            self._detail_source_handle.close()
        self._detail_source_handle = None
        self._detail_load_token += 1
        if self._detail_stream is not None:
            self._detail_stream.cancel()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._detail_warmup_client_id)
        self.detail_thumbnail_source_ready.emit(book_uuid, 0)
        self.prepare_detail_thumbnail_source(book_uuid, self._detail_thumbnail_size)

    def _book_in_current_shelf(self, book: Book) -> bool:
        if self.current_shelf == ShelfKey.HIDDEN:
            # The Hidden shelf is the only built-in surface that shows
            # ``is_hidden`` books at all.
            return book.is_hidden
        if book.is_hidden and not self._is_hidable_collection_shelf(self.current_shelf):
            # Hidden books vanish from ALL/Recent/Favourites and from
            # normal (non-hidable) user collections. The hidable-collection
            # branch below opts back in.
            return False
        if self.current_shelf == ShelfKey.ALL:
            return True
        if self.current_shelf == ShelfKey.RECENT:
            return book.last_read_at is not None
        if self.current_shelf == ShelfKey.FAVOURITES:
            return book.is_favourite
        collection_uuid = self._collection_uuid_from_shelf(self.current_shelf)
        return collection_uuid in book.collection_ids

    def _is_hidable_collection_shelf(self, shelf: str) -> bool:
        if not shelf.startswith("collection:"):
            return False
        collection_uuid = self._collection_uuid_from_shelf(shelf)
        for collection in self.collections:
            if collection.uuid == collection_uuid:
                return collection.is_hidable
        return False

    def _book_matches_filter(self, book: Book) -> bool:
        if self.file_filter == FileFilter.ALL:
            return True
        return book.file_format.upper() == self.file_filter.value.upper()

    def _book_matches_search(self, book: Book, query: BookSearchQuery) -> bool:
        document = self._search_documents_by_book_id.get(book.uuid)
        if document is None:
            # Search documents are cached because ``visible_books`` can be
            # recomputed many times during one render. Rebuild lazily if a
            # book appeared through a targeted refresh instead of load_books().
            document = self._build_search_document(book)
            self._search_documents_by_book_id[book.uuid] = document
        return matches_book_search(document, query)

    def _refresh_search_documents(self) -> None:
        """Pre-build a search document for every book on the shelf.

        Search matching uses tokenized, normalized fields (title/author).
        Building one document per typed character would be wasted work, so
        we cache them. This call is invoked after :meth:`load_books`;
        targeted updates use :meth:`_record_search_documents` instead.
        """

        self._search_documents_by_book_id = {
            book.uuid: self._build_search_document(book)
            for book in self.books
        }

    def _record_search_documents(self, books: Iterable[Book]) -> None:
        for book in books:
            self._search_documents_by_book_id[book.uuid] = self._build_search_document(book)

    def _build_search_document(self, book: Book) -> BookSearchDocument:
        return build_book_search_document(book.uuid, book.title, book.author)

    def _book_matches_tag_filter(self, book: Book) -> bool:
        if not self._tag_filter_ids:
            return True
        book_tag_ids = set(self._book_tag_ids_by_book.get(book.uuid, ()))
        return set(self._tag_filter_ids).issubset(book_tag_ids)

    def _refresh_book_tag_index(self) -> None:
        """Refresh the book → tag-ids lookup used by the tag filter.

        ``self._book_tag_ids_by_book`` powers :meth:`_book_matches_tag_filter`
        — keeping it in memory avoids hitting the tag repository per book on
        every filter pass. Repository failure is degraded (empty index) so
        the shelf can still render rows, just without tag-based filtering.
        """

        book_ids = tuple(book.uuid for book in self.books)
        if self._tag_service is None:
            self.available_tags = []
            self._book_tag_ids_by_book = {book_id: () for book_id in book_ids}
            return
        try:
            self.available_tags = self._tag_service.list_tags()
        except Exception as exc:  # pragma: no cover - repository-specific failure path.
            logger.warning("Shelf tag list lookup failed: %s", exc, exc_info=True)
            self.available_tags = []
        try:
            self._book_tag_ids_by_book = (
                self._tag_service.list_tag_ids_for_books(book_ids)
                if book_ids
                else {}
            )
        except Exception as exc:  # pragma: no cover - repository-specific failure path.
            logger.warning("Shelf book tag lookup failed: %s", exc, exc_info=True)
            self._book_tag_ids_by_book = {book_id: () for book_id in book_ids}

    def refresh_tags_async(self) -> None:
        """Reload the tag list off the UI thread and publish it when it lands.

        The tag dialogs read :attr:`available_tags` rather than querying the
        repository as they open, because that query blocks on the database
        actor's queue -- fast when it is idle, an outright freeze when an
        import or an audit is ahead of it. Keeping the list current is
        therefore this ViewModel's job, and it does the work on a worker.

        Falls back to the synchronous refresh when no executor was injected,
        which is the situation in focused tests rather than in the app.
        """

        if self._tag_service is None:
            self.available_tags = []
            return
        if self._task_service is None:
            self._refresh_book_tag_index()
            self._emit_state()
            return
        service = self._tag_service
        book_ids = tuple(book.uuid for book in self.books)
        self._tag_refresh_token += 1
        token = self._tag_refresh_token

        def load() -> tuple[list[Tag], dict[str, tuple[str, ...]]]:
            return (
                service.list_tags(),
                service.list_tag_ids_for_books(book_ids) if book_ids else {},
            )

        def publish(loaded: tuple[list[Tag], dict[str, tuple[str, ...]]]) -> None:
            # Refreshes can finish out of order -- creating two tags in quick
            # succession submits two reads, and the older one completing last
            # would republish a snapshot without the newer tag. The dialogs
            # read this cache exclusively, so that tag would simply be missing
            # until something else triggered a refresh.
            if token != self._tag_refresh_token or service is not self._tag_service:
                logger.debug("Dropping superseded tag refresh (token=%d)", token)
                return
            self.available_tags, self._book_tag_ids_by_book = loaded
            self._emit_state()

        def failed(error: Exception) -> None:
            # Degraded exactly as the synchronous path degrades: the shelf
            # still renders, just without tag data.
            logger.warning("Async shelf tag refresh failed: %s", error)

        self._task_service.submit("shelf-tags", load, on_success=publish, on_failure=failed)

    def _handle_book_tags_success(self, book_uuid: str) -> None:
        logger.info("Book tags assigned book=%s", book_uuid)
        self._refresh_book_tag_index()
        self._emit_state()

    def _handle_book_tags_failure(self, book_uuid: str, error: Exception) -> None:
        # See _handle_book_cover_failure: reconstructed exc_info so the sync
        # caller keeps its traceback and the queued on_failure caller doesn't
        # silently lose it under exc_info=True.
        logger.warning(
            "set_book_tag_ids failed book=%s: %s",
            book_uuid,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self._refresh_book_tag_index()
        self.book_tags_failed.emit(str(error))

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

    def _emit_missing(self, book_uuid: str, action: str) -> None:
        # Single emission site so every missing-book user-action path
        # leaves a matching trace line behind for log triage.
        logger.debug("missing_book_requested action=%s book=%s", action, book_uuid)
        self.missing_book_requested.emit(book_uuid)

    def _emit_unavailable(self, book_uuid: str, action: str, book: Book) -> None:
        if book.is_missing:
            self._emit_missing(book_uuid, action)
            return
        logger.debug("unavailable_book_requested action=%s book=%s", action, book_uuid)
        self.unavailable_book_requested.emit(book_uuid)

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
            self._record_search_documents((refreshed,))
            return refreshed
        if current == refreshed:
            return refreshed
        self.books = [refreshed if book.uuid == book_uuid else book for book in self.books]
        self._record_search_documents((refreshed,))
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
        """Refresh state and report the error without discarding valid state.

        A failed delete is not a delete: dropping the selection and closing the
        Detail page treated it as one, so the user lost their place and had to
        reselect before they could retry.

        The reload decides what survives rather than the request. That also
        covers a partial failure -- if some books were deleted before the error,
        they disappear from ``books`` and fall out of the selection here, while
        everything still present stays selected.
        """

        logger.warning("delete_books task failed books=%s: %s", target_ids, error)
        self.load_books()
        surviving = {book.uuid for book in self.books}
        self.selected_book_ids &= surviving
        if self.detail_book_uuid is not None and self.detail_book_uuid not in surviving:
            self._set_detail_book_uuid(None)
        self.selection_changed.emit(set(self.selected_book_ids))
        self.delete_failed.emit(str(error))

    def _record_cover(self, book_uuid: str, path: Path, *, force: bool = False) -> None:
        if not force and self._cover_paths.get(book_uuid) == path:
            return
        self._cover_paths[book_uuid] = path
        self.cover_ready.emit(book_uuid, path)

    def _handle_cover_result(self, book_uuid: str, path: Path | None) -> None:
        self._pending_cover_ids.discard(book_uuid)
        if path is None:
            logger.debug("Cover generation returned no path book=%s", book_uuid)
            return
        if not any(book.uuid == book_uuid for book in self.books):
            logger.debug("Cover generation result dropped for stale book=%s path=%s", book_uuid, path)
            return
        self._record_cover(book_uuid, path)

    def _handle_detail_source_result(
        self,
        token: int,
        book_uuid: str,
        source: ThumbnailSourceHandle | None,
        size: tuple[int, int],
    ) -> None:
        if token != self._detail_load_token or self.detail_book_uuid != book_uuid:
            if source is not None:
                source.close()
            return
        self._detail_source_task = None
        if self._detail_source_handle is not None and self._detail_source_handle is not source:
            self._detail_source_handle.close()
        self._detail_source_handle = source
        if source is None or self._detail_stream is None or self._thumbnail_service is None:
            self.detail_thumbnail_source_ready.emit(book_uuid, 0)
            return

        thumbnail_service = self._thumbnail_service

        def load(indices: tuple[int, ...], emit_item) -> None:  # noqa: ANN001
            thumbnail_service.stream_thumbnails(
                source,
                indices,
                size,
                lambda item: emit_item(ThumbnailStreamItem(item.page_index, item.image_bytes)),
            )

        planner = getattr(source, "plan_read_batch", None)
        self._detail_stream.set_source(
            source.source_id,
            source.page_count,
            size,
            load,
            batch_planner=planner if callable(planner) else None,
            batch_size_for=source.preferred_batch_size,
        )
        self.detail_thumbnail_source_ready.emit(book_uuid, source.page_count)
        visible, prefetch = self._detail_pending_interest
        if visible or prefetch:
            self._detail_stream.set_interest(visible, prefetch)
            self._ensure_detail_warmup()

    def _handle_detail_source_failure(self, token: int, error: Exception) -> None:
        if token != self._detail_load_token:
            return
        logger.warning("Detail thumbnail source failed: %s", error)
        self._detail_source_task = None
        if self.detail_book_uuid is not None:
            self.detail_thumbnail_source_ready.emit(self.detail_book_uuid, 0)

    def _handle_detail_stream_item(self, page_index: int, image_bytes: bytes) -> None:
        if self.detail_book_uuid is not None:
            self.page_thumbnail_ready.emit(self.detail_book_uuid, page_index, image_bytes)

    def _ensure_detail_warmup(self) -> None:
        source = self._detail_source_handle
        coordinator = self._archive_warmup_coordinator
        if source is None or coordinator is None:
            return
        access_mode = source.access_mode
        if getattr(access_mode, "value", access_mode) != "expensive_cold":
            return
        if not source.requires_sequential_warmup:
            return
        if source.persistent_cache_key is None:
            return
        coordinator.acquire(
            source.source_path,
            self._detail_warmup_client_id,
            limits=source.archive_limits,
            document_cache_key=source.persistent_cache_key,
            allow_persistent_cache=True,
            on_ready=lambda: self._detail_stream.refresh() if self._detail_stream is not None else None,
        )

    def _rebuild_detail_stream(self) -> None:
        if self._detail_stream is not None:
            self._detail_stream.cancel()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._detail_warmup_client_id)
        self._detail_stream = None
        if self._thumbnail_service is None or self._task_service is None:
            return
        issue_client = getattr(self._thumbnail_service, "issue_thumbnail_cache_client", None)
        if not callable(issue_client):
            return
        self._detail_stream = ThumbnailStreamController(
            self._task_service,
            issue_client("detail-page"),
            task_name="detail-thumbnail",
        )
        self._detail_stream.thumbnail_ready.connect(self._handle_detail_stream_item)

    def _set_detail_book_uuid(self, book_uuid: str | None) -> None:
        if self.detail_book_uuid == book_uuid:
            return
        if self._detail_source_task is not None:
            self._detail_source_task.cancel()
        self._detail_source_task = None
        if self._detail_source_handle is not None:
            self._detail_source_handle.close()
        self._detail_source_handle = None
        self._detail_pending_interest = ((), ())
        if self._detail_stream is not None:
            self._detail_stream.cancel()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._detail_warmup_client_id)
        self.detail_book_uuid = book_uuid
        self._detail_load_token += 1

    def resize_detail_thumbnail_cache(self, max_bytes: int) -> None:
        """Deprecated compatibility hook; the app cache owns this budget."""

        del max_bytes

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
