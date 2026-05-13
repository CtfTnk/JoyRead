"""Reader ViewModel: session state, navigation, layout, and persistence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from joyread.core.archive import ArchiveError, ArchivePasswordRejected, ArchivePasswordRequired
from joyread.core.reader import (
    ReaderDirection,
    ReaderDisplayMode,
    ReaderFitMode,
    ReaderImageSession,
    ReaderLayoutResult,
    ReaderPageImage,
    ReaderProgress,
    ReaderSessionService,
    ReaderSettings,
    ReaderTransitionMode,
    SizeF,
    SmartLayoutEngine,
)
from joyread.core.services.cache_service import NamespacedPageCache
from joyread.core.services.library_service import LibraryService
from joyread.core.services.task_service import TaskHandle, TaskService, TaskStatus
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.signals import Signal


@dataclass(frozen=True)
class ReaderPasswordPrompt:
    archive_path: str
    display_name: str
    message: str
    is_retry: bool = False


class ReaderViewModel:
    """Coordinates reader state without depending on PySide widgets."""

    def __init__(
        self,
        session_service: ReaderSessionService,
        task_service: TaskService,
        page_cache: NamespacedPageCache,
        library_service: LibraryService | None = None,
        *,
        book_uuid: str | None = None,
        title: str = "Reader",
        settings: ReaderSettings | None = None,
        progress: ReaderProgress | None = None,
        prefetch_before: int = 1,
        prefetch_after: int = 1,
        archive_internal_max_depth: int = 2,
    ) -> None:
        self.state_changed: Signal[None] = Signal()
        self.layout_changed: Signal[ReaderLayoutResult] = Signal()
        self.page_ready: Signal[ReaderPageImage] = Signal()
        self.error_changed: Signal[str | None] = Signal()
        self.password_required: Signal[ReaderPasswordPrompt] = Signal()
        self.progress_changed: Signal[tuple[str, int, float]] = Signal()

        self._session_service = session_service
        self._task_service = task_service
        # `_page_cache` namespaces the shared CacheService.reader_page_cache so
        # each reader window only sees its own pages and `cancel()` purges
        # just this session's bytes from the global budget.
        self._page_cache = page_cache
        self._library_service = library_service
        self._layout_engine = SmartLayoutEngine()
        self._session_lock = RLock()
        self._session: ReaderImageSession | None = None
        self._source_path: Path | None = None
        self._archive_passwords: dict[str, str] = {}
        self._skipped_archives: set[str] = set()
        self._pending_password_archive: str | None = None
        self._book_uuid = book_uuid
        self._open_handle: TaskHandle[ReaderImageSession] | None = None
        self._page_handles: dict[int, TaskHandle[ReaderPageImage | None]] = {}
        self._warm_handle: TaskHandle[None] | None = None
        self._save_handle: TaskHandle[None] | None = None
        self._task_generation = 0
        self._viewport_size = SizeF(1.0, 1.0)
        self._layout_result: ReaderLayoutResult | None = None
        self._pages: dict[int, ReaderPageImage] = {}
        self._unavailable_pages: set[int] = set()
        self._page_count = 0
        # Prefetch windows come from AppConfig; we hold the raw values and let
        # `_preload_nearby_pages` apply direction-aware bias so RTL readers
        # prefetch toward the next page, not the previous one.
        self._prefetch_before = max(0, int(prefetch_before))
        self._prefetch_after = max(0, int(prefetch_after))
        self._archive_internal_max_depth = max(1, min(5, int(archive_internal_max_depth)))
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

    @property
    def page_count(self) -> int:
        return self._page_count

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
        self._open_handle = self._task_service.submit(
            "reader-open",
            lambda: self._session_service.open_document(
                self._source_path or source_path,
                passwords=dict(self._archive_passwords),
                skipped_archives=set(self._skipped_archives),
                archive_internal_max_depth=self._archive_internal_max_depth,
            ),
            on_success=lambda session, generation=generation: self._handle_open_success(generation, session),
            on_failure=lambda error, generation=generation: self._handle_open_failure(generation, error),
        )

    def cancel(self, *, reset_passwords: bool = True) -> None:
        self._task_generation += 1
        if self._open_handle is not None:
            self._open_handle.cancel()
        for handle in self._page_handles.values():
            handle.cancel()
        if self._warm_handle is not None:
            self._warm_handle.cancel()
        if self._save_handle is not None:
            self._save_handle.cancel()
        self._open_handle = None
        self._page_handles.clear()
        self._warm_handle = None
        self._save_handle = None
        with self._session_lock:
            self._session = None
        self.is_loading = False
        self.loading_page_index = None
        self._layout_waiting_for_pages = ()
        self._pages.clear()
        self._unavailable_pages.clear()
        self._page_count = 0
        self._layout_result = None
        self._source_path = None
        if reset_passwords:
            self._archive_passwords.clear()
            self._skipped_archives.clear()
            self._pending_password_archive = None
        self._wide_pan_anchor = None
        self._wide_pan_user_panned = False
        # Free this session's slice of the shared reader page budget so other
        # open readers can claim it. The namespace itself stays valid; a
        # subsequent `open_path` will refill it.
        self._page_cache.clear()

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

    def set_viewport_size(self, width: int, height: int) -> None:
        size = SizeF(max(1.0, float(width)), max(1.0, float(height)))
        if size == self._viewport_size:
            return
        self._viewport_size = size
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
            self._request_page(indices[0])
            self._wait_for_layout_pages((indices[0],), status_index=indices[0])
            return
        page2 = self._pages.get(indices[1]) if len(indices) > 1 else None
        if len(indices) > 1 and page2 is None:
            self._request_page(indices[1])
            if indices[1] not in self._unavailable_pages:
                self._wait_for_layout_pages(indices, status_index=indices[0])
                return
            self._companion_index = None

        result = self._layout_engine.calculate(
            self._viewport_size,
            SizeF(float(page1.dimensions[0]), float(page1.dimensions[1])),
            SizeF(float(page2.dimensions[0]), float(page2.dimensions[1])) if page2 is not None else None,
            self.settings.layout_settings(),
            page1_index=indices[0],
            page2_index=indices[1] if len(indices) > 1 and page2 is not None else None,
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

    def _recalculate_vertical_layout(self) -> None:
        indices = self.current_display_indices
        if not indices:
            return
        primary = self._pages.get(self._primary_index)
        if primary is None:
            self._request_page(self._primary_index)
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
        step = self._vertical_step()
        if step <= 0:
            return True

        self._vertical_scroll_y += float(delta_y)
        changed_page = False
        while self._vertical_scroll_y <= -step and self._primary_index < self._page_count - 1:
            self._primary_index += 1
            self._vertical_scroll_y += step
            changed_page = True
        while self._vertical_scroll_y >= step and self._primary_index > 0:
            self._primary_index -= 1
            self._vertical_scroll_y -= step
            changed_page = True

        if self._primary_index <= 0 and self._vertical_scroll_y > 0:
            self._vertical_scroll_y = 0.0
        if self._primary_index >= self._page_count - 1 and self._vertical_scroll_y < 0:
            self._vertical_scroll_y = 0.0

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
        return (
            self._page_count > 1
            and not self.settings.always_one_page
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

    def _handle_open_success(self, generation: int, session: ReaderImageSession) -> None:
        if generation != self._task_generation:
            return
        self._session = session
        self._page_count = session.page_count
        self._primary_index = max(0, min(self._primary_index, max(0, self._page_count - 1)))
        self._companion_index = self._companion_for_seek(self._primary_index)
        self.is_loading = False
        self.error_message = None
        self._request_visible_pages()
        self.recalculate_layout()
        self._save_progress()
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
        if isinstance(error, ArchiveError):
            self.error_message = str(error)
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
        for handle in self._page_handles.values():
            handle.cancel()
        self._page_handles.clear()
        with self._session_lock:
            self._session = None
        self.is_loading = False
        self.loading_page_index = None
        self._layout_waiting_for_pages = ()
        self._layout_result = None
        self._pages.clear()
        self._unavailable_pages.clear()
        self._page_count = 0
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

    def _request_visible_pages(self) -> None:
        self._request_pages(self.current_display_indices)

    def _request_page(self, page_index: int) -> None:
        self._request_pages((page_index,))

    def _request_pages(self, page_indices: tuple[int, ...] | set[int]) -> None:
        if self._session is None:
            return

        missing: list[int] = []
        cached_images: list[ReaderPageImage] = []
        for page_index in dict.fromkeys(page_indices):
            if (
                not 0 <= page_index < self._page_count
                or page_index in self._pages
                or page_index in self._page_handles
            ):
                continue
            cached = self._page_cache.get(page_index)
            if cached is None:
                missing.append(page_index)
                continue
            with self._session_lock:
                dimensions = self._session.get_dimensions(page_index)
            if dimensions is not None:
                if self._should_keep_page_resident(page_index):
                    cached_images.append(ReaderPageImage(page_index, cached, dimensions))
                continue
            missing.append(page_index)

        for image in cached_images:
            self._store_page_loaded(image)

        if not missing:
            if cached_images:
                self.recalculate_layout()
            return

        requested = tuple(missing)
        generation = self._task_generation

        def load() -> dict[int, ReaderPageImage]:
            with self._session_lock:
                if self._session is None:
                    return {}
                return self._session_service.load_pages(self._session, requested)

        handle = self._task_service.submit(
            f"reader-pages-{requested[0]}",
            load,
            on_success=lambda images, generation=generation, requested=requested: self._handle_page_batch_success(
                generation, requested, images
            ),
            on_failure=lambda error, generation=generation, requested=requested: self._handle_page_batch_failure(
                generation, requested, error
            ),
        )
        for page_index in requested:
            if handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                self._page_handles[page_index] = handle
        if cached_images:
            self.recalculate_layout()

    def _handle_page_batch_success(
        self,
        generation: int,
        page_indices: tuple[int, ...],
        images: dict[int, ReaderPageImage],
    ) -> None:
        if generation != self._task_generation:
            return
        loaded = False
        for page_index in page_indices:
            self._page_handles.pop(page_index, None)
            image = images.get(page_index)
            if image is None:
                self._mark_page_unavailable(page_index)
                continue
            self._unavailable_pages.discard(page_index)
            self._page_cache.put(page_index, image.image_bytes)
            if self._should_keep_page_resident(image.page_index):
                self._store_page_loaded(image)
                loaded = True
        if loaded:
            self.recalculate_layout()

    def _handle_page_batch_failure(self, generation: int, page_indices: tuple[int, ...], error: Exception) -> None:
        if generation != self._task_generation:
            return
        for page_index in page_indices:
            self._page_handles.pop(page_index, None)
        if isinstance(error, (ArchivePasswordRejected, ArchivePasswordRequired)):
            self._request_password_retry(error)
            return
        for page_index in page_indices:
            self._mark_page_unavailable(page_index, error)

    def _mark_page_unavailable(self, page_index: int, error: Exception | None = None) -> None:
        self._unavailable_pages.add(page_index)
        if page_index == self._primary_index:
            detail = f": {error}" if error is not None else "."
            self.error_message = f"Could not load page {page_index + 1}{detail}"
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

    def _handle_page_loaded(self, image: ReaderPageImage) -> None:
        self._store_page_loaded(image)
        self.recalculate_layout()

    def _store_page_loaded(self, image: ReaderPageImage) -> None:
        self._pages[image.page_index] = image

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
        if self._source_path is None or self._warm_handle is not None:
            return
        if self._archive_passwords or self._skipped_archives:
            return
        should_warm = getattr(self._session_service, "should_warm_disk_cache", None)
        warm_disk_cache = getattr(self._session_service, "warm_disk_cache", None)
        if should_warm is None or warm_disk_cache is None:
            return
        if not should_warm(self._source_path):
            return
        source_path = self._source_path
        handle_ref: list[TaskHandle[None] | None] = [None]

        def is_cancelled() -> bool:
            handle = handle_ref[0]
            return handle is not None and handle.status == TaskStatus.CANCELLED

        handle = self._task_service.submit(
            "reader-cache-warm",
            lambda: warm_disk_cache(
                source_path,
                archive_internal_max_depth=self._archive_internal_max_depth,
                is_cancelled=is_cancelled,
            ),
        )
        handle_ref[0] = handle
        self._warm_handle = handle

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

    def _vertical_step(self) -> float:
        zoom = (
            max(25, min(200, int(self.settings.vertical_zoom_percent))) / 100.0
            if self.settings.vertical_custom_enabled
            else 1.0
        )
        gap = float(self.settings.page_spacing if self.settings.vertical_custom_enabled else 0)
        return (self._viewport_size.height * zoom) + gap

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

    def _persist_settings(self) -> None:
        if self._library_service is None or self._book_uuid is None:
            return
        settings = self.settings
        self._task_service.submit(
            "reader-settings",
            lambda: self._library_service.save_reader_settings(self._book_uuid or "", settings),
        )

    def _emit_state(self) -> None:
        self.state_changed.emit()


def _archive_display_name(archive_path: str) -> str:
    if "::" in archive_path:
        return archive_path
    try:
        return Path(archive_path).name or archive_path
    except (OSError, ValueError):
        return archive_path
