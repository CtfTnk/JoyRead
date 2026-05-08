"""Reader ViewModel: session state, navigation, layout, and persistence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import RLock

from joyread.core.archive import ArchiveError, ArchiveImageSession, ArchivePasswordRejected, ArchivePasswordRequired
from joyread.core.reader import (
    ReaderDirection,
    ReaderDisplayMode,
    ReaderFitMode,
    ReaderLayoutResult,
    ReaderPageImage,
    ReaderProgress,
    ReaderSessionService,
    ReaderSettings,
    ReaderTransitionMode,
    SizeF,
    SmartLayoutEngine,
)
from joyread.core.services.cache_service import CacheService
from joyread.core.services.library_service import LibraryService
from joyread.core.services.task_service import TaskHandle, TaskService
from joyread.ui.viewmodels.signals import Signal


class ReaderViewModel:
    """Coordinates reader state without depending on PySide widgets."""

    def __init__(
        self,
        session_service: ReaderSessionService,
        task_service: TaskService,
        cache_service: CacheService,
        library_service: LibraryService | None = None,
        *,
        book_uuid: str | None = None,
        title: str = "Reader",
        settings: ReaderSettings | None = None,
        progress: ReaderProgress | None = None,
    ) -> None:
        self.state_changed: Signal[None] = Signal()
        self.layout_changed: Signal[ReaderLayoutResult] = Signal()
        self.page_ready: Signal[ReaderPageImage] = Signal()
        self.error_changed: Signal[str | None] = Signal()
        self.password_required: Signal[str] = Signal()

        self._session_service = session_service
        self._task_service = task_service
        self._cache_service = cache_service
        self._library_service = library_service
        self._layout_engine = SmartLayoutEngine()
        self._session_lock = RLock()
        self._session: ArchiveImageSession | None = None
        self._source_path: Path | None = None
        self._source_signature = ""
        self._book_uuid = book_uuid
        self._open_handle: TaskHandle[ArchiveImageSession] | None = None
        self._page_handles: dict[int, TaskHandle[ReaderPageImage | None]] = {}
        self._save_handle: TaskHandle[None] | None = None
        self._viewport_size = SizeF(1.0, 1.0)
        self._layout_result: ReaderLayoutResult | None = None
        self._pages: dict[int, ReaderPageImage] = {}
        self._page_count = 0
        self._current_index = max(0, progress.page_index if progress is not None else 0)
        self._pan_x = 0.0

        self.title = title
        self.settings = settings or ReaderSettings()
        self.is_loading = False
        self.error_message: str | None = None

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def current_display_indices(self) -> tuple[int, ...]:
        if self._page_count <= 0:
            return ()
        start = max(0, min(self._current_index, self._page_count - 1))
        indices = [start]
        if (
            not self.settings.always_one_page
            and start + 1 < self._page_count
            and not (self.settings.spread_offset and start == 0)
        ):
            indices.append(start + 1)
        return tuple(indices)

    @property
    def layout_result(self) -> ReaderLayoutResult | None:
        return self._layout_result

    @property
    def pan_x(self) -> float:
        return self._pan_x

    def open_path(self, source_path: str | Path, password: str | None = None) -> None:
        self.cancel()
        self._source_path = Path(source_path)
        self._source_signature = _source_signature(self._source_path)
        self.is_loading = True
        self.error_message = None
        self._pages.clear()
        self._page_count = 0
        self._layout_result = None
        self._emit_state()
        self._open_handle = self._task_service.submit(
            "reader-open",
            lambda: self._session_service.open_archive(self._source_path or source_path, password=password),
            on_success=self._handle_open_success,
            on_failure=self._handle_open_failure,
        )

    def cancel(self) -> None:
        if self._open_handle is not None:
            self._open_handle.cancel()
        for handle in self._page_handles.values():
            handle.cancel()
        if self._save_handle is not None:
            self._save_handle.cancel()
        self._open_handle = None
        self._page_handles.clear()
        self._save_handle = None

    def set_viewport_size(self, width: int, height: int) -> None:
        size = SizeF(max(1.0, float(width)), max(1.0, float(height)))
        if size == self._viewport_size:
            return
        self._viewport_size = size
        self.recalculate_layout()

    def recalculate_layout(self) -> None:
        indices = self.current_display_indices
        if not indices:
            return
        page1 = self._pages.get(indices[0])
        if page1 is None:
            self._request_page(indices[0])
            return
        page2 = self._pages.get(indices[1]) if len(indices) > 1 else None
        if len(indices) > 1 and page2 is None:
            self._request_page(indices[1])

        result = self._layout_engine.calculate(
            self._viewport_size,
            SizeF(float(page1.dimensions[0]), float(page1.dimensions[1])),
            SizeF(float(page2.dimensions[0]), float(page2.dimensions[1])) if page2 is not None else None,
            self.settings.layout_settings(),
            page1_index=indices[0],
            page2_index=indices[1] if len(indices) > 1 and page2 is not None else None,
        )
        self._layout_result = result
        self._pan_x = max(result.pan_min_x, min(result.pan_max_x, self._pan_x))
        self.layout_changed.emit(result)
        self._preload_nearby_pages()

    def seek(self, page_index: int) -> None:
        if self._page_count <= 0:
            return
        next_index = max(0, min(page_index, self._page_count - 1))
        if next_index == self._current_index:
            return
        self._current_index = next_index
        self._pan_x = 0.0
        self._request_visible_pages()
        self.recalculate_layout()
        self._save_progress()
        self._emit_state()

    def jump_to_start(self) -> None:
        self.seek(0)

    def jump_to_end(self) -> None:
        if self._page_count > 0:
            self.seek(self._page_count - 1)

    def go_next(self) -> None:
        self.seek(self._current_index + self._current_step())

    def go_previous(self) -> None:
        self.seek(self._current_index - self._current_step())

    def handle_horizontal_key(self, side: str) -> None:
        if self._layout_result is not None and self._layout_result.mode == ReaderDisplayMode.WIDE_PAN:
            if self._pan_for_side(side):
                self.layout_changed.emit(self._layout_result)
                self._emit_state()
                return
        if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT:
            self.go_next() if side == "left" else self.go_previous()
        else:
            self.go_previous() if side == "left" else self.go_next()

    def activate_left_side(self) -> None:
        self.go_next() if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT else self.go_previous()

    def activate_right_side(self) -> None:
        self.go_previous() if self.settings.direction == ReaderDirection.RIGHT_TO_LEFT else self.go_next()

    def set_direction(self, direction: ReaderDirection) -> None:
        if direction == self.settings.direction:
            return
        self.settings = replace(
            self.settings,
            direction=direction,
            vertical_enabled=direction == ReaderDirection.TOP_TO_BOTTOM,
        )
        self._persist_settings()
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
        if self._current_index > 0:
            if next_offset and self._current_index % 2 == 0:
                self._current_index -= 1
            if not next_offset and self._current_index % 2 == 1:
                self._current_index -= 1
        self._persist_settings()
        self._request_visible_pages()
        self.recalculate_layout()
        self._emit_state()

    def set_custom_enabled(self, enabled: bool) -> None:
        self.settings = replace(self.settings, custom_enabled=enabled)
        self._persist_settings()
        self.recalculate_layout()
        self._emit_state()

    def set_always_one_page(self, enabled: bool) -> None:
        self.settings = replace(self.settings, always_one_page=enabled)
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

    def _handle_open_success(self, session: ArchiveImageSession) -> None:
        self._session = session
        self._page_count = session.page_count
        self._current_index = max(0, min(self._current_index, max(0, self._page_count - 1)))
        self.is_loading = False
        self.error_message = None
        self._request_visible_pages()
        self._emit_state()

    def _handle_open_failure(self, error: Exception) -> None:
        self.is_loading = False
        if isinstance(error, (ArchivePasswordRequired, ArchivePasswordRejected)):
            self.password_required.emit(str(error))
            self.error_message = "Password required."
        elif isinstance(error, ArchiveError):
            self.error_message = str(error)
        else:
            self.error_message = f"Could not open reader: {error}"
        self.error_changed.emit(self.error_message)
        self._emit_state()

    def _request_visible_pages(self) -> None:
        for index in self.current_display_indices:
            self._request_page(index)

    def _request_page(self, page_index: int) -> None:
        if page_index in self._pages or page_index in self._page_handles or self._session is None:
            return
        cached = self._cache_service.page_cache.get(self._cache_key(page_index))
        if cached is not None:
            with self._session_lock:
                dimensions = self._session.get_dimensions(page_index)
            if dimensions is not None:
                self._handle_page_loaded(ReaderPageImage(page_index, cached, dimensions))
                return

        def load() -> ReaderPageImage | None:
            with self._session_lock:
                if self._session is None:
                    return None
                return self._session_service.load_page(self._session, page_index)

        self._page_handles[page_index] = self._task_service.submit(
            f"reader-page-{page_index}",
            load,
            on_success=lambda image, page_index=page_index: self._handle_page_task_success(page_index, image),
            on_failure=lambda _error, page_index=page_index: self._page_handles.pop(page_index, None),
        )

    def _handle_page_task_success(self, page_index: int, image: ReaderPageImage | None) -> None:
        self._page_handles.pop(page_index, None)
        if image is None:
            return
        self._cache_service.page_cache.put(self._cache_key(page_index), image.image_bytes)
        self._handle_page_loaded(image)

    def _handle_page_loaded(self, image: ReaderPageImage) -> None:
        self._pages[image.page_index] = image
        self.page_ready.emit(image)
        self.recalculate_layout()

    def _preload_nearby_pages(self) -> None:
        if self._page_count <= 0:
            return
        for index in {
            max(0, self._current_index - 1),
            min(self._page_count - 1, self._current_index + 1),
            min(self._page_count - 1, self._current_index + self._current_step()),
        }:
            self._request_page(index)

    def _current_step(self) -> int:
        if self._layout_result is not None and self._layout_result.mode == ReaderDisplayMode.DOUBLE:
            return 2
        return 1

    def _pan_for_side(self, side: str) -> bool:
        result = self._layout_result
        if result is None or not result.supports_horizontal_pan:
            return False
        step = max(ThemeLessReaderPan.MIN_STEP, self._viewport_size.width * ThemeLessReaderPan.STEP_RATIO)
        next_pan = self._pan_x + (-step if side == "left" else step)
        next_pan = max(result.pan_min_x, min(result.pan_max_x, next_pan))
        if next_pan == self._pan_x:
            return False
        self._pan_x = next_pan
        return True

    def _save_progress(self) -> None:
        if self._library_service is None or self._book_uuid is None or self._page_count <= 0:
            return
        percent = 0.0 if self._page_count <= 1 else (self._current_index / (self._page_count - 1)) * 100.0
        self._save_handle = self._task_service.submit(
            "reader-progress",
            lambda: self._library_service.set_progress(self._book_uuid or "", self._current_index, percent),
        )

    def _persist_settings(self) -> None:
        if self._library_service is None or self._book_uuid is None:
            return
        settings = self.settings
        self._task_service.submit(
            "reader-settings",
            lambda: self._library_service.save_reader_settings(self._book_uuid or "", settings),
        )

    def _cache_key(self, page_index: int) -> str:
        return f"{self._source_signature}:{page_index}"

    def _emit_state(self) -> None:
        self.state_changed.emit()


class ThemeLessReaderPan:
    """Small constants kept out of Theme to avoid importing UI tokens here."""

    STEP_RATIO = 0.18
    MIN_STEP = 80.0


def _source_signature(path: Path | None) -> str:
    if path is None:
        return "unknown"
    try:
        stat = path.stat()
    except OSError:
        return str(path)
    return f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
