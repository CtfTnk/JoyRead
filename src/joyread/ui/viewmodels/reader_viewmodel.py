"""Reader ViewModel: session state, navigation, layout, and persistence."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from joyread.app.reader_document_runtime import ReaderDocumentHandle, ReaderDocumentRuntime
from joyread.app.reader_page_pipeline import (
    EncodedPageFrameDecoder,
    PageFrameDecoder,
    PreparedReaderPage,
    ReaderPagePipeline,
)

from joyread.core.archive import (
    ArchiveError,
    ArchiveOpenLimits,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveResourceLimitError,
)
from joyread.core.models.bookmark import Bookmark
from joyread.core.reader import (
    ReaderDirection,
    ReaderDisplayMode,
    ReaderFitMode,
    ReaderLayoutResult,
    ReaderProgress,
    ReaderSettings,
    ReaderTransitionMode,
    SizeF,
    SmartLayoutEngine,
)
from joyread.core.services.cache_service import (
    NamespacedPageCache,
    SharedThumbnailCache,
    ThumbnailCacheClient,
    ThumbnailSourceIdentity,
)
from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.core.services.library_service import LibraryService
from joyread.app.tasking import TaskExecutor, TaskHandle, TaskPriority, TaskStatus
from joyread.app.thumbnail_stream import ThumbnailStreamController, ThumbnailStreamItem
from joyread.core.services.thumbnail_service import ThumbnailRenderer
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.signals import Signal


logger = logging.getLogger(__name__)


def _archive_error_message(error: ArchiveError) -> str:
    """Map structured archive failures without leaking backend details into UI."""

    if isinstance(error, ArchiveResourceLimitError):
        return t("reader.archive_resource_limit_exceeded")
    return str(error)


@dataclass(frozen=True)
class ReaderPasswordPrompt:
    archive_path: str
    display_name: str
    message: str
    is_retry: bool = False


@dataclass(frozen=True)
class ReaderBookmarkItem:
    uuid: str
    name: str
    page_index: int


@dataclass(frozen=True)
class ReaderContentsItem:
    """One TOC entry surfaced to the topic panel's CONTENTS mode.

    ``page_index`` is an opaque seek target: an archive page index in the
    manga reader and a spine index in the EPUB reader.
    """

    label: str
    page_index: int
    depth: int = 0


class ReaderViewModel:
    """Coordinates reader state without depending on PySide widgets.

    The reader coordinates two execution domains:

    1. The Qt UI thread, which calls ``open_path``, ``go_next``,
       ``set_viewport_size``, etc.
    2. Task workers, which open documents, read/decompress pages, decode and
       scale frames, hash external sources, and persist progress.

    ``ReaderPagePipeline`` owns document open/close, the replaceable page task,
    and request tokens. ``ReaderDocumentRuntime`` owns Core sessions, archive
    cache leases, and external hash promotion; the ViewModel only keeps the
    resulting Application-layer document handle for UI state and commands.

    The production Qt executor marshals success, failure, and item callbacks to
    the GUI thread before ViewModel signals update widgets.
    """

    def __init__(
        self,
        document_runtime: ReaderDocumentRuntime,
        task_service: TaskExecutor,
        page_cache: NamespacedPageCache,
        library_service: LibraryService | None = None,
        *,
        book_uuid: str | None = None,
        title: str = "Reader",
        settings: ReaderSettings | None = None,
        progress: ReaderProgress | None = None,
        prefetch_before: int = 1,
        prefetch_after: int = 1,
        nested_archive_max_depth: int = 2,
        archive_global_file_max_depth: int = 100,
        archive_limits: ArchiveOpenLimits | None = None,
        thumbnail_cache_client: ThumbnailCacheClient | None = None,
        archive_warmup_coordinator: ArchiveWarmupCoordinator | None = None,
        page_decoder: PageFrameDecoder | None = None,
        thumbnail_renderer: ThumbnailRenderer | None = None,
    ) -> None:
        self.state_changed: Signal[None] = Signal()
        self.layout_changed: Signal[ReaderLayoutResult] = Signal()
        self.page_ready: Signal[PreparedReaderPage] = Signal()
        self.page_failed: Signal[int] = Signal()
        self.error_changed: Signal[str | None] = Signal()
        self.password_required: Signal[ReaderPasswordPrompt] = Signal()
        self.progress_changed: Signal[tuple[str, int, float]] = Signal()
        self.bookmarks_changed: Signal[tuple[ReaderBookmarkItem, ...]] = Signal()
        self.contents_changed: Signal[tuple[ReaderContentsItem, ...]] = Signal()
        self.bookmark_error_changed: Signal[str] = Signal()
        self.topic_thumbnail_ready: Signal[tuple[int, bytes]] = Signal()

        self._document_runtime = document_runtime
        self._task_service = task_service
        # `_page_cache` namespaces the shared CacheService.reader_page_cache so
        # each reader window only sees its own pages and `cancel()` purges
        # just this session's bytes from the global budget.
        self._page_cache = page_cache
        self._thumbnail_renderer = thumbnail_renderer
        self._library_service = library_service
        self._archive_warmup_coordinator = archive_warmup_coordinator
        self._warmup_client_id = f"reader:{id(self)}"
        self._layout_engine = SmartLayoutEngine()
        self._document: ReaderDocumentHandle | None = None
        self._source_path: Path | None = None
        self._archive_passwords: dict[str, str] = {}
        self._skipped_archives: set[str] = set()
        self._pending_password_archive: str | None = None
        self._book_uuid = book_uuid
        self._hash_handle: TaskHandle[str | None] | None = None
        self._save_handle: TaskHandle[None] | None = None
        self._settings_save_handle: TaskHandle[None] | None = None
        self._pending_settings_save: ReaderSettings | None = None
        self._bookmark_handle: TaskHandle[tuple[ReaderBookmarkItem, ...]] | None = None
        # Bumped on every `cancel()`/`open_path()`. Worker callbacks compare
        # against this snapshot before mutating state, so a page-load that
        # finishes after the user has navigated to a different book (or
        # closed the reader) silently drops its result instead of stomping
        # the canvas with stale pixels from the previous document.
        self._task_generation = 0
        self._viewport_size = SizeF(1.0, 1.0)
        self._layout_result: ReaderLayoutResult | None = None
        self._pages: dict[int, PreparedReaderPage] = {}
        self._unavailable_pages: set[int] = set()
        self._bookmarks: tuple[ReaderBookmarkItem, ...] = ()
        self._contents: tuple[ReaderContentsItem, ...] = ()
        # Production injects an app-scope client. The fallback keeps pure VM
        # tests independent without restoring the old fixed 32MB cache.
        thumbnail_client = thumbnail_cache_client or SharedThumbnailCache(64 * 1024 * 1024).issue_client(
            "reader-topic-test"
        )
        self._topic_thumbnail_stream = ThumbnailStreamController(
            task_service,
            thumbnail_client,
            task_name="reader-topic-thumbnail",
        )
        self._topic_thumbnail_stream.thumbnail_ready.connect(self.topic_thumbnail_ready.emit)
        self._page_count = 0
        # Prefetch windows come from AppConfig; we hold the raw values and let
        # `_preload_nearby_pages` apply direction-aware bias so RTL readers
        # prefetch toward the next page, not the previous one.
        self._prefetch_before = max(0, int(prefetch_before))
        self._prefetch_after = max(0, int(prefetch_after))
        self._archive_limits = archive_limits or ArchiveOpenLimits(
            nested_archive_max_depth=_core_depth_limit(
                nested_archive_max_depth,
                default=2,
                maximum=5,
            ),
            global_file_max_depth=_core_depth_limit(
                archive_global_file_max_depth,
                default=100,
                maximum=1000,
            ),
        )
        # `_primary_index` is the LAYOUT anchor only. It decides which page's
        # aspect ratio drives single/double/wide-pan layout. It is intentionally
        # NOT the user-facing reading position: the indicator, slider value,
        # resume index, and progress percent are derived from the actually
        # displayed indices (see `_navigation_anchor_indices`).
        self._primary_index = max(0, progress.page_index if progress is not None else 0)
        self._companion_index: int | None = None
        self._pan_x = 0.0
        self._wide_pan_anchor: tuple[int, ReaderDirection] | None = None
        self._wide_pan_user_panned = False
        self._vertical_scroll_y = 0.0
        # Last `(page_index, percent)` tuple submitted to persistence. Used to
        # de-duplicate the new layout-settled progress saves so we do not spam
        # the task service when nothing changed.
        self._last_saved_progress: tuple[int, float] | None = None

        self.title = title
        self.settings = settings or ReaderSettings()
        self.is_loading = False
        self.loading_page_index: int | None = None
        self._layout_waiting_for_pages: tuple[int, ...] = ()
        self.error_message: str | None = None
        self._device_pixel_ratio = 1.0
        self._page_pipeline = ReaderPagePipeline(
            task_service,
            page_decoder or EncodedPageFrameDecoder(),
            page_cache,
            on_ready=self._handle_page_prepared,
            on_failed=self._handle_page_prepare_failed,
        )

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def can_use_bookmarks(self) -> bool:
        return self._library_service is not None and self._book_uuid is not None

    @property
    def bookmarks(self) -> tuple[ReaderBookmarkItem, ...]:
        return self._bookmarks

    @property
    def contents(self) -> tuple[ReaderContentsItem, ...]:
        return self._contents

    @property
    def can_use_contents(self) -> bool:
        return bool(self._contents)

    @property
    def current_index(self) -> int:
        # User-facing reading position: smallest of the actually displayed
        # indices. Drives the footer indicator, the slider value, and the
        # resume index persisted to the database.
        if self._page_count <= 0:
            return 0
        anchors = self._navigation_anchor_indices()
        return min(anchors) if anchors else 0

    @property
    def current_display_indices(self) -> tuple[int, ...]:
        if self._page_count <= 0:
            return ()
        if self._is_vertical_mode:
            return tuple(
                index
                for index in range(self._primary_index - 1, self._primary_index + 3)
                if 0 <= index < self._page_count
            )
        primary = max(0, min(self._primary_index, self._page_count - 1))
        indices = [primary]
        if self._can_use_companion() and self._companion_index is not None:
            companion = max(0, min(self._companion_index, self._page_count - 1))
            if companion != primary:
                indices.append(companion)
        return tuple(indices)

    @property
    def layout_result(self) -> ReaderLayoutResult | None:
        return self._layout_result

    @property
    def pan_x(self) -> float:
        return self._pan_x

    @property
    def is_right_to_left(self) -> bool:
        return self.settings.direction == ReaderDirection.RIGHT_TO_LEFT

    @property
    def _is_vertical_mode(self) -> bool:
        return self.settings.direction == ReaderDirection.TOP_TO_BOTTOM

    def open_path(self, source_path: str | Path, password: str | None = None) -> None:
        path = Path(source_path)
        logger.info(
            "ReaderViewModel open_path book=%s path=%s with_password=%s",
            self._book_uuid,
            path,
            password is not None,
        )
        if self._source_path != path and password is None:
            self._archive_passwords.clear()
            self._skipped_archives.clear()
            self._pending_password_archive = None
        if password is not None:
            archive_path = self._pending_password_archive or str(path)
            self._archive_passwords[archive_path] = password
            self._skipped_archives.discard(archive_path)
            self._pending_password_archive = None
        self.cancel(reset_passwords=False)
        self._source_path = path
        self.is_loading = True
        self.error_message = None
        self._pages.clear()
        self._unavailable_pages.clear()
        self._page_count = 0
        self._layout_result = None
        self.loading_page_index = None
        self._layout_waiting_for_pages = ()
        self._last_saved_progress = None
        self._emit_state()
        generation = self._task_generation
        open_work = lambda: self._document_runtime.open_document(
            self._source_path or source_path,
            passwords=dict(self._archive_passwords),
            skipped_archives=set(self._skipped_archives),
            limits=self._archive_limits,
        )
        self._page_pipeline.open_document(
            open_work,
            generation=generation,
            on_opened=lambda document, generation=generation: self._handle_open_success(
                generation,
                document,
            ),
            on_failed=lambda error, generation=generation: self._handle_open_failure(
                generation,
                error,
            ),
        )

    def cancel(self, *, reset_passwords: bool = True) -> None:
        logger.debug(
            "ReaderViewModel cancel book=%s reset_passwords=%s",
            self._book_uuid,
            reset_passwords,
        )
        self._task_generation += 1
        if self._hash_handle is not None:
            self._hash_handle.cancel()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._warmup_client_id)
        if self._save_handle is not None:
            self._save_handle.cancel()
        # Reader preferences are intentionally not cancelled here. They are
        # tiny per-book writes and should survive closing the reader window.
        if self._bookmark_handle is not None:
            self._bookmark_handle.cancel()
        self._hash_handle = None
        self._save_handle = None
        self._bookmark_handle = None
        # Both of these hand back *shared* budget, not just local state, and
        # neither says so at the call site.
        #
        # Cancelling the topic stream releases its thumbnail-cache client.
        # Pinned thumbnails are unevictable, so a client that is never released
        # leaves a closed reader holding part of the shared thumbnail cache for
        # the rest of the process.
        self._topic_thumbnail_stream.cancel()
        # `clear_cache=True` clears the pipeline's frame cache, which *is* this
        # session's `NamespacedPageCache` -- that is what frees this reader's
        # slice of the shared page budget. The namespace itself stays valid; a
        # subsequent `open_path` refills it.
        self._page_pipeline.cancel(clear_cache=True)
        self._document = None
        self.is_loading = False
        self.loading_page_index = None
        self._layout_waiting_for_pages = ()
        self._pages.clear()
        self._unavailable_pages.clear()
        self._bookmarks = ()
        self._set_contents(())
        self._page_count = 0
        self._layout_result = None
        self._source_path = None
        if reset_passwords:
            self._archive_passwords.clear()
            self._skipped_archives.clear()
            self._pending_password_archive = None
        self._wide_pan_anchor = None
        self._wide_pan_user_panned = False
        self.bookmarks_changed.emit(self._bookmarks)

    def cancel_password_request(self) -> None:
        self._pending_password_archive = None
        self._archive_passwords.clear()
        self._skipped_archives.clear()
        self.is_loading = False
        self.loading_page_index = None
        self._layout_waiting_for_pages = ()
        self._layout_result = None
        self.error_message = "Could not load images because the archive is encrypted and no password was provided."
        self.error_changed.emit(self.error_message)
        self._emit_state()

    def skip_password_request(self) -> None:
        source_path = self._source_path
        archive_path = self._pending_password_archive or (str(source_path) if source_path is not None else "")
        self._pending_password_archive = None
        if archive_path:
            self._archive_passwords.pop(archive_path, None)
            self._skipped_archives.add(archive_path)
        if source_path is None:
            self.error_message = "No readable images. Encrypted archives were skipped."
            self.error_changed.emit(self.error_message)
            self._emit_state()
            return
        self.open_path(source_path)

    def refresh_bookmarks(self) -> None:
        if not self.can_use_bookmarks:
            self._set_bookmarks(())
            return
        self._submit_bookmark_task(
            "reader-bookmarks-load",
            lambda service, book_uuid: _bookmark_items(service.list_bookmarks(book_uuid)),
            refresh_on_failure=False,
        )

    def add_bookmark(self) -> None:
        if not self.can_use_bookmarks or self._page_count <= 0:
            return
        page_index = self.current_index
        default_name = self.title.strip() or t("reader.bookmark_default")

        def work(service: LibraryService, book_uuid: str) -> tuple[ReaderBookmarkItem, ...]:
            service.add_bookmark(book_uuid, default_name, page_index)
            return _bookmark_items(service.list_bookmarks(book_uuid))

        self._submit_bookmark_task("reader-bookmark-add", work)

    def rename_bookmark(self, bookmark_uuid: str, name: str) -> None:
        cleaned = name.strip()
        if not self.can_use_bookmarks or not bookmark_uuid or not cleaned:
            return

        def work(service: LibraryService, book_uuid: str) -> tuple[ReaderBookmarkItem, ...]:
            service.rename_bookmark(book_uuid, bookmark_uuid, cleaned)
            return _bookmark_items(service.list_bookmarks(book_uuid))

        self._submit_bookmark_task("reader-bookmark-rename", work)

    def delete_bookmark(self, bookmark_uuid: str) -> None:
        if not self.can_use_bookmarks or not bookmark_uuid:
            return

        def work(service: LibraryService, book_uuid: str) -> tuple[ReaderBookmarkItem, ...]:
            service.delete_bookmark(book_uuid, bookmark_uuid)
            return _bookmark_items(service.list_bookmarks(book_uuid))

        self._submit_bookmark_task("reader-bookmark-delete", work)

    def set_topic_thumbnail_interest(
        self,
        visible_indices: tuple[int, ...],
        prefetch_indices: tuple[int, ...],
        size: tuple[int, int],
    ) -> None:
        if self._document is None or self._page_count <= 0:
            self._topic_thumbnail_stream.release_interest()
            return
        if self._topic_thumbnail_stream.source_id is None:
            self._configure_topic_thumbnail_stream(self._document, size)
        self._topic_thumbnail_stream.set_interest(visible_indices, prefetch_indices)

    def release_topic_thumbnail_interest(self) -> None:
        self._topic_thumbnail_stream.release_interest()

    def set_viewport_size(
        self,
        width: int,
        height: int,
        device_pixel_ratio: float | None = None,
    ) -> None:
        size = SizeF(max(1.0, float(width)), max(1.0, float(height)))
        dpr = self._device_pixel_ratio if device_pixel_ratio is None else max(1.0, float(device_pixel_ratio))
        if size == self._viewport_size and dpr == self._device_pixel_ratio:
            return
        self._viewport_size = size
        self._device_pixel_ratio = dpr
        target = (max(1, round(size.width * dpr)), max(1, round(size.height * dpr)))
        for page_index in self.current_display_indices:
            page = self._pages.get(page_index)
            if page is not None and _prepared_frame_is_too_small(page, target):
                self._pages.pop(page_index, None)
        self.recalculate_layout()

    def recalculate_layout(self) -> None:
        if self._is_vertical_mode:
            self._recalculate_vertical_layout()
            return
        indices = self.current_display_indices
        if not indices:
            return
        page1 = self._pages.get(indices[0])
        if page1 is None:
            # ``_request_page`` may resolve synchronously from the LRU
            # cache, in which case it already recursed into
            # ``recalculate_layout`` and emitted a layout. Re-check before
            # falling through to the wait state to avoid clobbering that
            # layout with a "loading" placeholder.
            self._request_page(indices[0])
            page1 = self._pages.get(indices[0])
            if page1 is None:
                self._wait_for_layout_pages((indices[0],), status_index=indices[0])
                return
        page2 = self._pages.get(indices[1]) if len(indices) > 1 else None
        if len(indices) > 1 and page2 is None:
            self._request_page(indices[1])
            page2 = self._pages.get(indices[1])
            if page2 is None:
                if indices[1] in self._unavailable_pages:
                    self._companion_index = None
                # The primary page is immediately useful. Render it as a
                # provisional single page while the companion independently
                # finishes, then the next item callback upgrades to DOUBLE.

        page2_index = indices[1] if len(indices) > 1 and page2 is not None else None
        laid_out = (indices[0],) if page2_index is None else (indices[0], page2_index)
        result = self._layout_engine.calculate(
            self._viewport_size,
            SizeF(float(page1.dimensions[0]), float(page1.dimensions[1])),
            SizeF(float(page2.dimensions[0]), float(page2.dimensions[1])) if page2 is not None else None,
            self.settings.layout_settings(),
            page1_index=indices[0],
            page2_index=page2_index,
            sticky_mode=self._sticky_layout_mode(laid_out),
        )
        self._finish_layout_loading()
        self._layout_result = result
        self._sync_wide_pan_for_layout(result)
        self._prune_resident_pages()
        self.layout_changed.emit(result)
        self._emit_ready_pages_for_layout(result)
        self._preload_nearby_pages()
        self._start_disk_cache_warmup()
        # Re-save once the layout actually settles. Before this point we only
        # had the planned `(primary, companion)` pair; after layout, page draws
        # may have collapsed to SINGLE/WIDE_PAN and the largest displayed index
        # used for progress percent can differ. De-duplication in
        # `_save_progress` keeps this idempotent.
        self._save_progress()

    def _sticky_layout_mode(self, laid_out: tuple[int, ...]) -> ReaderDisplayMode | None:
        """The on-screen mode, but only when it belongs to these exact pages.

        The engine uses this to damp its pairing decision across a resize, so
        it has to describe the spread being recalculated and no other. Handing
        it a mode carried over from a different spread would make that spread's
        layout depend on what was read before it.

        Draw order follows reading direction rather than page order, so the
        comparison sorts both sides.
        """

        result = self._layout_result
        if result is None or not result.page_draws:
            return None
        drawn = tuple(sorted(draw.page_index for draw in result.page_draws))
        if drawn != tuple(sorted(laid_out)):
            return None
        return result.mode

    def _recalculate_vertical_layout(self) -> None:
        indices = self.current_display_indices
        if not indices:
            return
        primary = self._pages.get(self._primary_index)
        if primary is None:
            self._request_page(self._primary_index)
            primary = self._pages.get(self._primary_index)
            if primary is None:
                self._wait_for_layout_pages((self._primary_index,), status_index=self._primary_index)
                return

        pages: list[tuple[int, SizeF]] = []
        primary_size = SizeF(float(primary.dimensions[0]), float(primary.dimensions[1]))
        for page_index in indices:
            image = self._pages.get(page_index)
            if image is None:
                if page_index in self._unavailable_pages:
                    continue
                self._request_page(page_index)
                # Vertical mode can keep the scroll structure stable while
                # secondary visible pages load by drawing page-sized loading
                # placeholders. The primary page is already loaded, so its
                # dimensions are a reasonable temporary viewport-local stand-in.
                pages.append((page_index, primary_size))
                continue
            pages.append((page_index, SizeF(float(image.dimensions[0]), float(image.dimensions[1]))))

        result = self._layout_engine.calculate_vertical(
            self._viewport_size,
            tuple(pages),
            self.settings.layout_settings(),
            anchor_index=self._primary_index,
            scroll_y=self._vertical_scroll_y,
        )
        self._finish_layout_loading()
        self._layout_result = result
        self._pan_x = 0.0
        self._wide_pan_anchor = None
        self._wide_pan_user_panned = False
        self._prune_resident_pages()
        self.layout_changed.emit(result)
        self._emit_ready_pages_for_layout(result)
        self._preload_nearby_pages()
        self._start_disk_cache_warmup()
        # Vertical mode always anchors on `_primary_index`, so the saved values
        # rarely change here. The call still goes through the de-dup guard.
        self._save_progress()

    def seek(self, page_index: int) -> None:
        if self._page_count <= 0:
            return
        next_index = max(0, min(page_index, self._page_count - 1))
        next_companion = self._companion_for_seek(next_index)
        if next_index == self._primary_index and next_companion == self._companion_index:
            return
        self._set_target_spread(next_index, next_companion)
        self._save_progress()
        self._emit_state()

    def jump_to_start(self) -> None:
        self.seek(0)

    def jump_to_end(self) -> None:
        if self._page_count > 0:
            # Jump-to-end is an explicit user intent to land on the last page;
            # render it as a single-page spread so the indicator reads
            # `N/N` and progress is unambiguously 100%.
            self._set_target_spread(self._page_count - 1, None)
            self._save_progress()
            self._emit_state()

    def go_next(self) -> None:
        if self._page_count <= 0:
            return
        if self._step_navigation_is_blocked():
            return
        if self._is_vertical_mode:
            self.seek(self._primary_index + 1)
            return
        current_high = max(self._navigation_anchor_indices())
        next_primary = current_high + 1
        if next_primary >= self._page_count:
            return
        self._set_target_spread(next_primary, self._next_companion(next_primary))
        self._save_progress()
        self._emit_state()

    def go_previous(self) -> None:
        if self._page_count <= 0:
            return
        if self._step_navigation_is_blocked():
            return
        if self._is_vertical_mode:
            self.seek(self._primary_index - 1)
            return
        current_low = min(self._navigation_anchor_indices())
        if current_low <= 0:
            return
        previous_primary = max(0, current_low - 1)
        self._set_target_spread(previous_primary, self._previous_companion(previous_primary))
        self._save_progress()
        self._emit_state()

    def handle_horizontal_key(self, side: str) -> None:
        if self._pan_visible_wide_page(side):
            return
        if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT:
            self.go_next() if side == "left" else self.go_previous()
        else:
            self.go_previous() if side == "left" else self.go_next()

    def activate_left_side(self) -> None:
        if self._pan_visible_wide_page("left"):
            return
        self.go_next() if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT else self.go_previous()

    def activate_right_side(self) -> None:
        if self._pan_visible_wide_page("right"):
            return
        self.go_previous() if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT else self.go_next()

    def set_direction(self, direction: ReaderDirection) -> None:
        if direction == self.settings.direction:
            return
        self.settings = replace(self.settings, direction=direction)
        self._vertical_scroll_y = 0.0
        self._refresh_companion_for_primary()
        self._persist_settings()
        self._request_visible_pages()
        self.recalculate_layout()
        self._emit_state()

    def set_transition_mode(self, mode: ReaderTransitionMode) -> None:
        if mode == self.settings.transition_mode:
            return
        self.settings = replace(self.settings, transition_mode=mode)
        self._persist_settings()
        self._emit_state()

    def toggle_spread_offset(self) -> None:
        next_offset = 0 if self.settings.spread_offset else 1
        self.settings = replace(self.settings, spread_offset=next_offset)
        if self._primary_index > 0:
            if next_offset and self._primary_index % 2 == 0:
                self._primary_index -= 1
            if not next_offset and self._primary_index % 2 == 1:
                self._primary_index -= 1
        self._companion_index = self._next_companion(self._primary_index)
        self._persist_settings()
        self._request_visible_pages()
        self.recalculate_layout()
        self._emit_state()

    def shift_to_next_index(self) -> None:
        # Advance the smallest displayed index by one regardless of which page
        # is currently the layout anchor. This matches the user-visible mental
        # model: pressing "shift" walks the spread window one archive index
        # forward (e.g. `[0, 1]` -> `[1, 2]`, and even after a backward step
        # such as `(1, 0)` it correctly moves on to `(1, 2)`).
        if self._page_count <= 0:
            return
        anchors = self._navigation_anchor_indices()
        if not anchors:
            return
        self.seek(min(anchors) + 1)

    def set_custom_enabled(self, enabled: bool) -> None:
        self.settings = replace(self.settings, custom_enabled=enabled)
        # When the master toggle flips, ``always_one_page`` may go from
        # active (force single page) to neutralised (allow spreads) or the
        # other way round, so the companion slot needs to follow.
        self._refresh_companion_for_primary()
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def set_always_one_page(self, enabled: bool) -> None:
        self.settings = replace(self.settings, always_one_page=enabled)
        self._refresh_companion_for_primary()
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def set_fit_mode(self, fit_mode: ReaderFitMode) -> None:
        self.settings = replace(self.settings, fit_mode=fit_mode)
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def set_page_spacing(self, spacing: int) -> None:
        self.settings = replace(self.settings, page_spacing=max(0, spacing))
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def set_vertical_custom_enabled(self, enabled: bool) -> None:
        self.settings = replace(self.settings, vertical_custom_enabled=enabled)
        self._vertical_scroll_y = 0.0
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def set_vertical_fit_width(self, enabled: bool) -> None:
        self.settings = replace(self.settings, vertical_fit_width=enabled)
        self._vertical_scroll_y = 0.0
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def set_vertical_zoom_percent(self, value: int) -> None:
        clamped = max(25, min(200, int(value)))
        self.settings = replace(self.settings, vertical_zoom_percent=clamped)
        self._vertical_scroll_y = 0.0
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def handle_vertical_scroll(self, delta_y: int) -> bool:
        if not self._is_vertical_mode or self._page_count <= 0:
            return False
        self._vertical_scroll_y += float(delta_y)
        changed_page = False
        while self._primary_index < self._page_count - 1:
            step = self._vertical_step(self._primary_index)
            if step <= 0 or self._vertical_scroll_y > -step:
                break
            self._primary_index += 1
            self._vertical_scroll_y += step
            changed_page = True
        while self._primary_index > 0:
            step = self._vertical_step(self._primary_index - 1)
            if step <= 0 or self._vertical_scroll_y < step:
                break
            self._primary_index -= 1
            self._vertical_scroll_y -= step
            changed_page = True

        if self._primary_index <= 0 and self._vertical_scroll_y > 0:
            self._vertical_scroll_y = 0.0
        if self._primary_index >= self._page_count - 1 and self._vertical_scroll_y < 0:
            # On the last page there's no further page to transition into,
            # so the natural floor is "page bottom meets viewport bottom".
            # Only the *lower* bound is clamped here: positive scroll_y
            # has to keep flowing so the backward-transition loop above
            # can commit to the previous page once the user scrolls up
            # past a full page step. When the last page fits the viewport
            # ``_last_page_min_scroll`` returns 0, recovering the previous
            # "lock at page top" behaviour.
            min_scroll = self._last_page_min_scroll()
            if self._vertical_scroll_y < min_scroll:
                self._vertical_scroll_y = min_scroll

        if changed_page:
            self._save_progress()
            self._emit_state()
        self._request_visible_pages()
        self.recalculate_layout()
        return True

    def _set_target_spread(self, primary_index: int, companion_index: int | None) -> None:
        self._primary_index = max(0, min(primary_index, max(0, self._page_count - 1)))
        self._companion_index = self._valid_companion(companion_index)
        self._pan_x = 0.0
        self._wide_pan_anchor = None
        self._wide_pan_user_panned = False
        self._vertical_scroll_y = 0.0
        self._request_visible_pages()
        self.recalculate_layout()

    def _refresh_companion_for_primary(self) -> None:
        self._companion_index = self._companion_for_seek(self._primary_index)
        self._wide_pan_anchor = None
        self._wide_pan_user_panned = False

    def _can_use_companion(self) -> bool:
        # ``always_one_page`` only forces the single-page slot when the
        # master "Enable Custom" toggle is on, matching how the layout
        # engine reads the same flag.
        force_single = self.settings.custom_enabled and self.settings.always_one_page
        return (
            self._page_count > 1
            and not force_single
            and self.settings.direction != ReaderDirection.TOP_TO_BOTTOM
        )

    def _companion_for_seek(self, primary_index: int) -> int | None:
        # Only pair with the next page in archive order. Falling back to the
        # previous page would force `seek(last_index)` (slider drag, resume on
        # the last page, or `shift_to_next_index` arriving at the end) to
        # render `[last - 1, last]` again, which is indistinguishable from
        # the natural forward spread and confuses both the indicator and the
        # user. Returning `None` here lets `_set_target_spread` collapse to a
        # single-page display when the user explicitly lands on the last
        # archive index.
        return self._next_companion(primary_index)

    def _next_companion(self, primary_index: int) -> int | None:
        return self._valid_companion(primary_index + 1, primary_index)

    def _previous_companion(self, primary_index: int) -> int | None:
        return self._valid_companion(primary_index - 1, primary_index)

    def _valid_companion(self, companion_index: int | None, primary_index: int | None = None) -> int | None:
        if companion_index is None or not self._can_use_companion():
            return None
        primary = self._primary_index if primary_index is None else primary_index
        if not 0 <= companion_index < self._page_count:
            return None
        if companion_index == primary or companion_index in self._unavailable_pages:
            return None
        return companion_index

    def _handle_open_success(self, generation: int, document: ReaderDocumentHandle) -> None:
        if generation != self._task_generation:
            document.close()
            return
        self._document = document
        self._page_count = document.page_count
        self._configure_topic_thumbnail_stream(
            document,
            (Theme.detail_thumbnail_width, Theme.detail_thumbnail_height),
        )
        archive_contents = document.contents
        self._set_contents(
            tuple(
                ReaderContentsItem(
                    label=str(entry.label),
                    page_index=int(entry.page_index),
                    depth=max(0, int(entry.depth)),
                )
                for entry in archive_contents
            )
        )
        self._primary_index = max(0, min(self._primary_index, max(0, self._page_count - 1)))
        self._companion_index = self._companion_for_seek(self._primary_index)
        self.is_loading = False
        self.error_message = None
        self._request_visible_pages()
        self.recalculate_layout()
        self._save_progress()
        self.refresh_bookmarks()
        self._emit_state()

    def _handle_open_failure(self, generation: int, error: Exception) -> None:
        if generation != self._task_generation:
            return
        if isinstance(error, ArchivePasswordRejected):
            self._request_password_retry(error)
            return
        if isinstance(error, ArchivePasswordRequired):
            self._request_password_retry(error)
            return
        self.is_loading = False
        self._set_contents(())
        if isinstance(error, ArchiveError):
            self.error_message = _archive_error_message(error)
        else:
            self.error_message = f"Could not open reader: {error}"
        self.error_changed.emit(self.error_message)
        self._emit_state()

    def _request_password_retry(self, error: ArchivePasswordRejected | ArchivePasswordRequired) -> None:
        archive_path = getattr(error, "archive_path", None) or str(self._source_path or "")
        self._pending_password_archive = archive_path or None
        if isinstance(error, ArchivePasswordRejected) and archive_path:
            self._archive_passwords.pop(archive_path, None)
            self._skipped_archives.discard(archive_path)
        self._page_pipeline.cancel(clear_cache=True)
        self._document = None
        self.is_loading = False
        self.loading_page_index = None
        self._layout_waiting_for_pages = ()
        self._layout_result = None
        self._pages.clear()
        self._unavailable_pages.clear()
        self._page_count = 0
        self._set_contents(())
        display_name = _archive_display_name(archive_path)
        if isinstance(error, ArchivePasswordRejected):
            prompt = ReaderPasswordPrompt(
                archive_path=archive_path,
                display_name=display_name,
                message=f"Incorrect password for {display_name}. Please try again.",
                is_retry=True,
            )
        else:
            prompt = ReaderPasswordPrompt(
                archive_path=archive_path,
                display_name=display_name,
                message=f"Password required for {display_name}.",
                is_retry=False,
            )
        self.error_message = prompt.message
        self.error_changed.emit(self.error_message)
        self.layout_changed.emit(None)  # type: ignore[arg-type]
        self.password_required.emit(prompt)
        self._emit_state()

    def _submit_bookmark_task(
        self,
        name: str,
        work: Callable[[LibraryService, str], tuple[ReaderBookmarkItem, ...]],
        *,
        refresh_on_failure: bool = True,
    ) -> None:
        if self._library_service is None or self._book_uuid is None:
            self._set_bookmarks(())
            return
        if self._bookmark_handle is not None and self._bookmark_handle.status in {
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
        }:
            self._bookmark_handle.cancel()
        generation = self._task_generation
        service = self._library_service
        book_uuid = self._book_uuid
        self._bookmark_handle = self._task_service.submit(
            name,
            lambda: work(service, book_uuid),
            on_success=lambda bookmarks, generation=generation: self._handle_bookmark_success(
                generation,
                bookmarks,
            ),
            on_failure=lambda error, generation=generation, refresh=refresh_on_failure: (
                self._handle_bookmark_failure(generation, error, refresh_on_failure=refresh)
            ),
        )

    def _handle_bookmark_success(
        self,
        generation: int,
        bookmarks: tuple[ReaderBookmarkItem, ...],
    ) -> None:
        if generation != self._task_generation:
            return
        self._bookmark_handle = None
        self._set_bookmarks(bookmarks)

    def _handle_bookmark_failure(
        self,
        generation: int,
        error: Exception,
        *,
        refresh_on_failure: bool,
    ) -> None:
        if generation != self._task_generation:
            return
        self._bookmark_handle = None
        self.bookmark_error_changed.emit(t("reader.bookmark_sync_failed", error=str(error)))
        if refresh_on_failure:
            self.refresh_bookmarks()

    def _set_bookmarks(self, bookmarks: tuple[ReaderBookmarkItem, ...]) -> None:
        self._bookmarks = tuple(sorted(bookmarks, key=lambda item: (item.page_index, item.name, item.uuid)))
        self.bookmarks_changed.emit(self._bookmarks)

    def _configure_topic_thumbnail_stream(
        self,
        document: ReaderDocumentHandle,
        size: tuple[int, int],
    ) -> None:
        source_path = self._source_path
        renderer = self._thumbnail_renderer
        if source_path is None or renderer is None:
            return
        source_id = self._topic_thumbnail_source_id()
        def load(indices: tuple[int, ...], emit_item) -> None:  # noqa: ANN001
            missing: list[int] = []
            for page_index in indices:
                prepared = self._page_cache.get(page_index)
                if prepared is None or _prepared_frame_is_too_small(prepared, size):
                    missing.append(page_index)
                    continue
                try:
                    rendered = renderer.render_prepared(prepared.frame, size)
                except Exception as exc:
                    logger.debug("Prepared topic frame was not reusable page=%d: %s", page_index, exc)
                    missing.append(page_index)
                    continue
                emit_item(ThumbnailStreamItem(page_index, rendered))

            pages = document.read_pages(tuple(missing)) if missing else {}
            for page_index in missing:
                image = pages.get(page_index)
                if image is None:
                    continue
                try:
                    rendered = renderer.render_encoded(image.image_bytes, size)
                except Exception as exc:
                    logger.warning("Topic thumbnail render failed page=%d: %s", page_index, exc)
                    continue
                emit_item(ThumbnailStreamItem(page_index, rendered))

        self._topic_thumbnail_stream.set_source(
            source_id,
            document.page_count,
            size,
            load,
            batch_planner=document.plan_read_batch,
        )

    def _topic_thumbnail_source_id(self) -> str:
        """Return a cache identity scoped to this managed file or reader session.

        Source paths and their filesystem metadata are deliberately excluded:
        managed documents are identified by ``file_id`` and externally opened
        files are isolated by the random session key created at construction.
        """
        security_scope = (
            f"session-auth-{self._task_generation}"
            if self._archive_passwords or self._skipped_archives
            else "shared"
        )
        return ThumbnailSourceIdentity(
            self._document.cache_key if self._document is not None else "closed",
            self._archive_limits.cache_signature(),
            security_scope,
        ).cache_id

    def _request_visible_pages(self) -> None:
        self._request_pages(self.current_display_indices)

    def _request_page(self, page_index: int) -> None:
        self._request_pages((page_index,))

    def _request_pages(self, page_indices: tuple[int, ...] | set[int]) -> None:
        if self._document is None:
            return
        requested = tuple(
            index
            for index in dict.fromkeys(page_indices)
            if 0 <= index < self._page_count and index not in self._pages
        )
        if not requested:
            return
        visible_set = set(self.current_display_indices)
        visible = tuple(index for index in requested if index in visible_set)
        prefetch = tuple(index for index in requested if index not in visible_set)
        self._page_pipeline.request(
            visible,
            prefetch,
            target_width=max(1, round(self._viewport_size.width)),
            target_height=max(1, round(self._viewport_size.height)),
            device_pixel_ratio=self._device_pixel_ratio,
            generation=self._task_generation,
        )

    def _handle_page_prepared(self, page: PreparedReaderPage) -> None:
        if page.generation != self._task_generation:
            return
        self._unavailable_pages.discard(page.page_index)
        if self._should_keep_page_resident(page.page_index):
            self._pages[page.page_index] = page
            self.recalculate_layout()
        if page.page_index in self.current_display_indices:
            self._maybe_start_external_hash_promotion()

    def _handle_page_prepare_failed(
        self,
        page_index: int,
        error: Exception,
        generation: int,
    ) -> None:
        if generation != self._task_generation:
            return
        if isinstance(error, (ArchivePasswordRejected, ArchivePasswordRequired)):
            self._request_password_retry(error)
            return
        self._mark_page_unavailable(page_index, error)

    def _mark_page_unavailable(self, page_index: int, error: Exception | None = None) -> None:
        self._unavailable_pages.add(page_index)
        self.page_failed.emit(page_index)
        if page_index == self._primary_index:
            detail = f": {_archive_error_message(error)}" if error is not None else "."
            self.error_message = t("reader.page_load_failed", page=str(page_index + 1), detail=detail)
            self._layout_result = None
            self.loading_page_index = None
            self._layout_waiting_for_pages = ()
            self.error_changed.emit(self.error_message)
            self.layout_changed.emit(None)  # type: ignore[arg-type]
            self._emit_state()
            return
        if page_index == self._companion_index:
            self._companion_index = None
            self.recalculate_layout()

    def _emit_ready_pages_for_layout(self, result: ReaderLayoutResult) -> None:
        for draw in result.page_draws:
            image = self._pages.get(draw.page_index)
            if image is not None:
                self.page_ready.emit(image)

    def _should_keep_page_resident(self, page_index: int) -> bool:
        return page_index in self._resident_page_indices()

    def _resident_page_indices(self) -> set[int]:
        indices = set(self.current_display_indices)
        indices.update(self._layout_waiting_for_pages)
        if self._layout_result is not None:
            indices.update(draw.page_index for draw in self._layout_result.page_draws)
        return {index for index in indices if 0 <= index < self._page_count}

    def _prune_resident_pages(self) -> None:
        keep = self._resident_page_indices()
        for page_index in tuple(self._pages):
            if page_index not in keep:
                self._pages.pop(page_index, None)

    def _wait_for_layout_pages(self, page_indices: tuple[int, ...], *, status_index: int) -> None:
        waiting = tuple(dict.fromkeys(page_indices))
        if (
            self._layout_result is None
            and self.loading_page_index == status_index
            and self._layout_waiting_for_pages == waiting
        ):
            return
        self._layout_result = None
        self._pan_x = 0.0
        self._wide_pan_anchor = None
        self._wide_pan_user_panned = False
        self.loading_page_index = status_index
        self._layout_waiting_for_pages = waiting
        self._prune_resident_pages()
        self.layout_changed.emit(None)  # type: ignore[arg-type]
        self._emit_state()

    def _finish_layout_loading(self) -> None:
        if self.loading_page_index is None and not self._layout_waiting_for_pages:
            return
        self.loading_page_index = None
        self._layout_waiting_for_pages = ()
        self._emit_state()

    def _step_navigation_is_blocked(self) -> bool:
        return self.loading_page_index is not None

    def _start_disk_cache_warmup(self) -> None:
        coordinator = self._archive_warmup_coordinator
        if self._source_path is None or coordinator is None:
            return
        document = self._document
        if document is None:
            return
        if self._layout_result is None:
            # Nothing is on screen yet. Background conversion of a large solid
            # archive runs its own extractor, and starting it now would put two
            # of them on the machine while the reader is still waiting for its
            # first page. External sources reach here from hash promotion,
            # which can finish first; every layout pass calls this again, so
            # returning is a deferral, not a cancellation.
            return
        cache_key = document.warmup_cache_key
        if cache_key is None:
            return
        access_mode = document.access_mode
        if getattr(access_mode, "value", access_mode) != "expensive_cold":
            return
        if not document.requires_sequential_warmup:
            return
        coordinator.acquire(
            self._source_path,
            self._warmup_client_id,
            limits=self._archive_limits,
            document_cache_key=cache_key,
            allow_persistent_cache=True,
            # Warmup opens its own session and cannot inherit ours, so an
            # encrypted document has to be handed the password or it warms
            # nothing at all. Only the top-level archive's, which is the one
            # the conversion needs; nested-archive passwords stay here.
            password=self._archive_passwords.get(str(self._source_path)),
            on_ready=self._topic_thumbnail_stream.refresh,
        )

    def _maybe_start_external_hash_promotion(self) -> None:
        source_path = self._source_path
        document = self._document
        if (
            source_path is None
            or document is None
            or self._hash_handle is not None
            or not document.needs_external_cache_promotion
        ):
            return
        generation = self._task_generation

        def promote() -> str | None:
            return document.promote_external_cache(
                lambda: generation != self._task_generation
            )

        def complete(cache_key: str | None) -> None:
            self._hash_handle = None
            if generation != self._task_generation or cache_key is None:
                return
            self._topic_thumbnail_stream.promote_source(self._topic_thumbnail_source_id())
            self._start_disk_cache_warmup()

        def fail(error: Exception) -> None:
            self._hash_handle = None
            logger.warning("External archive cache promotion failed: %s", type(error).__name__)

        try:
            handle = self._task_service.submit(
                "reader-external-hash",
                promote,
                on_success=complete,
                on_failure=fail,
                priority=TaskPriority.LOW,
            )
        except TypeError:
            handle = self._task_service.submit(
                "reader-external-hash",
                promote,
                on_success=complete,
                on_failure=fail,
            )
        self._hash_handle = (
            handle if handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING} else None
        )

    def _preload_nearby_pages(self) -> None:
        if self._page_count <= 0:
            return
        # `before`/`after` follow archive index order, but RTL users read with
        # decreasing indices visually, so we swap the bias so prefetch always
        # tracks the direction the user is moving in.
        before, after = self._directional_prefetch_window()
        if self._is_vertical_mode:
            self._request_pages({
                index
                for index in range(self._primary_index - before, self._primary_index + after + 1)
                if 0 <= index < self._page_count
            })
            return

        step = self._current_step()
        targets: set[int] = set()
        # Pages behind the spread (going back).
        for offset in range(1, before + 1):
            targets.add(max(0, self._primary_index - offset))
        # Pages ahead of the spread. Walk ``after`` indices past the spread
        # so a forward turn always finds the next pages in the cache.
        for offset in range(1, after + 1):
            targets.add(min(self._page_count - 1, self._primary_index + step + offset - 1))
        self._request_pages(targets)

    def _directional_prefetch_window(self) -> tuple[int, int]:
        before = self._prefetch_before
        after = self._prefetch_after
        if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT:
            return after, before
        return before, after

    def _current_step(self) -> int:
        if self._layout_result is not None and self._layout_result.mode == ReaderDisplayMode.DOUBLE:
            return 2
        return 1

    def _navigation_anchor_indices(self) -> tuple[int, ...]:
        """Indices that are actually being displayed.

        Prefers the layout result's page draws (authoritative once a layout has
        been computed for the current spread) and falls back to the planned
        `current_display_indices` (or the clamped `_primary_index`) when the
        result is not yet ready or is stale. This is the single source of
        truth for three derived values:

        - navigation deltas in `go_next` / `go_previous`,
        - the smallest displayed index used by `current_index` (indicator,
          slider value, resume page) and `shift_to_next_index`,
        - the largest displayed index used by `_save_progress` for the percent.

        Staleness check: every horizontal layout the engine produces includes
        `_primary_index` in its `page_draws` (SINGLE/DOUBLE/WIDE_PAN all set
        `page1_index=_primary_index`). If `_primary_index` is missing, the
        cached layout result belongs to a previous spread whose pages were
        rendered before navigation advanced — typical between a navigation
        event and the async page-load that lets `recalculate_layout` finish.
        Returning those stale draws would desync the indicator from the
        upcoming canvas, so we fall back to the planned indices instead.
        """
        if self._is_vertical_mode:
            return (max(0, min(self._primary_index, max(0, self._page_count - 1))),)
        if self._layout_result is not None and self._layout_result.page_draws:
            draws = tuple(draw.page_index for draw in self._layout_result.page_draws)
            if self._primary_index in draws:
                return draws
        planned = self.current_display_indices
        if planned:
            return planned
        return (max(0, min(self._primary_index, max(0, self._page_count - 1))),)

    def _vertical_step(self, page_index: int) -> float:
        gap = float(self.settings.page_spacing if self.settings.vertical_custom_enabled else 0)
        if self.settings.vertical_custom_enabled and self.settings.vertical_fit_width:
            image = self._pages.get(page_index)
            if image is not None:
                width, height = image.dimensions
                if width > 0 and height > 0 and self._viewport_size.width > 0:
                    return (self._viewport_size.width * (float(height) / float(width))) + gap
            return self._viewport_size.height + gap
        zoom = (
            max(25, min(200, int(self.settings.vertical_zoom_percent))) / 100.0
            if self.settings.vertical_custom_enabled
            else 1.0
        )
        return (self._viewport_size.height * zoom) + gap

    def _last_page_min_scroll(self) -> float:
        # Lower bound for ``_vertical_scroll_y`` on the last page: a
        # negative offset that lets the page's bottom edge land at the
        # viewport's bottom edge. Returns ``0.0`` (no scroll past page
        # top) when the page is short enough to fit the viewport, so a
        # short last page still anchors at the top.
        if self._page_count <= 0 or self._viewport_size.height <= 0:
            return 0.0
        last_index = self._page_count - 1
        gap = float(self.settings.page_spacing if self.settings.vertical_custom_enabled else 0)
        page_height = max(0.0, self._vertical_step(last_index) - gap)
        if page_height <= 0:
            return 0.0
        return min(0.0, self._viewport_size.height - page_height)

    def _sync_wide_pan_for_layout(self, result: ReaderLayoutResult) -> None:
        if not result.supports_horizontal_pan:
            self._pan_x = 0.0
            self._wide_pan_anchor = None
            self._wide_pan_user_panned = False
            return

        anchor = (self._primary_index, self.settings.direction)
        if self._wide_pan_anchor != anchor or not self._wide_pan_user_panned:
            self._pan_x = self._wide_pan_start(result)
            self._wide_pan_anchor = anchor
            return
        self._pan_x = max(result.pan_min_x, min(result.pan_max_x, self._pan_x))

    def _wide_pan_start(self, result: ReaderLayoutResult) -> float:
        if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT:
            return result.pan_min_x
        return result.pan_max_x

    def _pan_visible_wide_page(self, side: str) -> bool:
        if self._layout_result is not None and self._layout_result.mode == ReaderDisplayMode.WIDE_PAN:
            if self._pan_for_side(side):
                self.layout_changed.emit(self._layout_result)
                self._emit_state()
                return True
        return False

    def _pan_for_side(self, side: str) -> bool:
        result = self._layout_result
        if result is None or not result.supports_horizontal_pan:
            return False
        step = max(Theme.reader_pan_min_step, self._viewport_size.width * Theme.reader_pan_step_ratio)
        if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT:
            delta = step if side == "left" else -step
        else:
            delta = -step if side == "right" else step
        next_pan = self._pan_x + delta
        next_pan = max(result.pan_min_x, min(result.pan_max_x, next_pan))
        if next_pan == self._pan_x:
            return False
        self._pan_x = next_pan
        self._wide_pan_user_panned = True
        return True

    def _save_progress(self) -> None:
        if self._library_service is None or self._book_uuid is None or self._page_count <= 0:
            return
        anchors = self._navigation_anchor_indices()
        if not anchors:
            return
        # Resume index = smallest displayed page; progress percent uses the
        # largest displayed page so a spread like `[N-2, N-1]` is recorded as
        # 100% complete even though `N-2` is the index we resume on.
        page_index = min(anchors)
        largest = max(anchors)
        percent = 0.0 if self._page_count <= 1 else (largest / (self._page_count - 1)) * 100.0
        if self._last_saved_progress == (page_index, percent):
            return
        self._last_saved_progress = (page_index, percent)
        book_uuid = self._book_uuid
        self._save_handle = self._task_service.submit(
            "reader-progress",
            lambda: self._library_service.set_progress(book_uuid, page_index, percent),
            on_success=lambda _result: self.progress_changed.emit(book_uuid, page_index, percent),
        )

    def flush_pending_writes(self) -> tuple[TaskHandle[None], ...]:
        """Submit any outstanding durable write and return what to await.

        Closing a Reader normally goes through :meth:`cancel`, which cancels
        the progress handle -- fine when the application keeps running, because
        the next save will catch up. A storage transition has no next save: the
        database this Reader writes to is about to be replaced. So the writes
        are pushed out first and the caller awaits them *before* anything is
        cancelled.

        Reuses the ordinary save paths rather than writing directly, so the
        deduplication in ``_save_progress`` still holds and a Reader with
        nothing dirty returns no handles at all.

        Settings saves stay serialized: a queued one is submitted only once its
        predecessor is done, exactly as during normal operation. Submitting
        both at once would race two writes for the same book and could persist
        the older one last. The caller therefore re-asks until this returns
        nothing, rather than treating one answer as the whole flush.
        """

        self._save_progress()
        if self._pending_settings_save is not None and not self._settings_save_in_flight():
            self._submit_pending_settings_save()
        handles = (self._save_handle, self._settings_save_handle)
        return tuple(
            handle
            for handle in handles
            if handle is not None
            and handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}
        )

    def _persist_settings(self) -> None:
        if self._library_service is None or self._book_uuid is None:
            return
        self._pending_settings_save = self.settings
        if self._settings_save_in_flight():
            return
        self._submit_pending_settings_save()

    def _settings_save_in_flight(self) -> bool:
        return self._settings_save_handle is not None and self._settings_save_handle.status in {
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
        }

    def _submit_pending_settings_save(self) -> None:
        if self._library_service is None or self._book_uuid is None or self._pending_settings_save is None:
            return
        settings = self._pending_settings_save
        self._pending_settings_save = None
        service = self._library_service
        book_uuid = self._book_uuid
        logger.debug(
            "Persisting reader settings book=%s direction=%s fit=%s vertical_custom=%s",
            book_uuid,
            settings.direction.value,
            settings.fit_mode.value,
            settings.vertical_custom_enabled,
        )
        self._settings_save_handle = self._task_service.submit(
            "reader-settings",
            lambda: service.save_reader_settings(book_uuid, settings),
            on_success=lambda _result, settings=settings: self._handle_settings_save_finished(settings),
            on_failure=lambda error, settings=settings, book_uuid=book_uuid: self._handle_settings_save_failed(
                book_uuid, settings, error
            ),
        )

    def _handle_settings_save_failed(
        self,
        book_uuid: str,
        settings: ReaderSettings,
        error: Exception,
    ) -> None:
        # The task service already logs the traceback at ERROR. Surface a
        # reader-side line so the failed save is tied to the book + setting
        # snapshot it tried to persist (this is the call site that hid the
        # ``vertical_fit_width`` schema-drift OperationalError).
        logger.error(
            "Persist reader settings failed book=%s direction=%s: %s",
            book_uuid,
            settings.direction.value,
            error,
        )
        self._handle_settings_save_finished(settings)

    def _handle_settings_save_finished(self, saved_settings: ReaderSettings) -> None:
        self._settings_save_handle = None
        if self._pending_settings_save is None:
            return
        if self._pending_settings_save == saved_settings:
            self._pending_settings_save = None
            return
        self._submit_pending_settings_save()

    def _emit_state(self) -> None:
        self.state_changed.emit()

    def _set_contents(self, contents: tuple[ReaderContentsItem, ...]) -> None:
        if contents == self._contents:
            return
        self._contents = contents
        self.contents_changed.emit(contents)


def _archive_display_name(archive_path: str) -> str:
    if "::" in archive_path:
        return archive_path
    try:
        return Path(archive_path).name or archive_path
    except (OSError, ValueError):
        return archive_path


def _bookmark_items(bookmarks: list[Bookmark]) -> tuple[ReaderBookmarkItem, ...]:
    return tuple(ReaderBookmarkItem(bookmark.uuid, bookmark.name, bookmark.page_index) for bookmark in bookmarks)


def _normalize_depth_limit(value: object, *, default: int, maximum: int) -> int:
    try:
        depth = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if depth == -1:
        return -1
    if depth < 1:
        return default
    return min(maximum, depth)


def _core_depth_limit(value: object, *, default: int, maximum: int) -> int | None:
    normalized = _normalize_depth_limit(value, default=default, maximum=maximum)
    return None if normalized == -1 else normalized


def _prepared_frame_is_too_small(
    page: PreparedReaderPage,
    target: tuple[int, int],
) -> bool:
    source_width, source_height = page.source_dimensions
    if source_width <= 0 or source_height <= 0:
        return False
    scale = min(target[0] / source_width, target[1] / source_height, 1.0)
    expected = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
    rendered_width, rendered_height = page.rendered_dimensions
    return (
        rendered_width * 1.2 < expected[0]
        or rendered_height * 1.2 < expected[1]
    )
