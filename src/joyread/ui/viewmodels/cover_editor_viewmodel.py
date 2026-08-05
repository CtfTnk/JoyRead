"""Viewport thumbnail state for the cover editor page picker."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from joyread.app.cover_editor import CoverPreviewRenderer, PreparedCoverSource
from joyread.core.models.book import Book
from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.app.thumbnail_stream import ThumbnailStreamController, ThumbnailStreamItem
from joyread.app.tasking import TaskExecutor, TaskHandle, TaskPriority, TaskStatus
from joyread.core.services.thumbnail_service import CoverCropState, ThumbnailService, ThumbnailSourceHandle
from joyread.ui.viewmodels.signals import Signal


@dataclass(frozen=True)
class _LoadedCoverSource:
    encoded_bytes: bytes
    prepared: PreparedCoverSource[object]
    opening: bool


class CoverEditorThumbnailViewModel:
    """Keeps cover-page browsing work outside the MainWindow view."""

    def __init__(
        self,
        thumbnail_service: ThumbnailService,
        task_service: TaskExecutor,
        preview_renderer: CoverPreviewRenderer[object],
        archive_warmup_coordinator: ArchiveWarmupCoordinator | None = None,
    ) -> None:
        self.source_ready: Signal[tuple[str, int]] = Signal("cover-editor.source_ready")
        self.thumbnail_ready: Signal[tuple[int, bytes]] = Signal("cover-editor.thumbnail_ready")
        self.failed: Signal[Exception] = Signal("cover-editor.failed")
        self.preview_ready: Signal[tuple[str, PreparedCoverSource[object], bool]] = Signal(
            "cover-editor.preview_ready"
        )
        self.preview_failed: Signal[tuple[Exception, bool]] = Signal("cover-editor.preview_failed")
        self.cover_saved: Signal[tuple[str, Path]] = Signal("cover-editor.cover_saved")
        self.save_failed: Signal[Exception] = Signal("cover-editor.save_failed")
        self._thumbnail_service = thumbnail_service
        self._task_service = task_service
        self._preview_renderer = preview_renderer
        self._archive_warmup_coordinator = archive_warmup_coordinator
        self._warmup_client_id = f"cover-editor-thumbnail:{id(self)}"
        self._stream = ThumbnailStreamController(
            task_service,
            thumbnail_service.issue_thumbnail_cache_client("cover-editor"),
            task_name="cover-editor-thumbnail",
        )
        self._stream.thumbnail_ready.connect(self.thumbnail_ready.emit)
        self._book: Book | None = None
        self._source: ThumbnailSourceHandle | None = None
        self._source_task: TaskHandle[ThumbnailSourceHandle | None] | None = None
        self._preview_task: TaskHandle[_LoadedCoverSource] | None = None
        self._save_task: TaskHandle[Path] | None = None
        self._source_bytes: bytes | None = None
        self._source_token: str | None = None
        self._pending_page_preview: tuple[int, tuple[int, int], bool] | None = None
        self._page_preview_request: tuple[int, tuple[int, int], bool] | None = None
        self._size = (1, 1)
        self._pending_interest: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
        self._generation = 0
        self._preview_generation = 0

    def replace_thumbnail_service(self, thumbnail_service: ThumbnailService) -> None:
        if thumbnail_service is self._thumbnail_service:
            return
        self.cancel()
        self._thumbnail_service = thumbnail_service
        self._stream = ThumbnailStreamController(
            self._task_service,
            thumbnail_service.issue_thumbnail_cache_client("cover-editor"),
            task_name="cover-editor-thumbnail",
        )
        self._stream.thumbnail_ready.connect(self.thumbnail_ready.emit)

    def set_book(self, book: Book | None, size: tuple[int, int]) -> None:
        if self._book is not None and book is not None and self._book.uuid == book.uuid:
            return
        self.cancel()
        self._book = book
        self._size = (max(1, int(size[0])), max(1, int(size[1])))
        if book is not None:
            self._open_source()

    def set_interest(
        self,
        visible_indices: tuple[int, ...],
        prefetch_indices: tuple[int, ...],
        size: tuple[int, int],
    ) -> None:
        self._size = (max(1, int(size[0])), max(1, int(size[1])))
        self._pending_interest = (visible_indices, prefetch_indices)
        if self._source is None:
            self._open_source()
            return
        self._stream.set_interest(visible_indices, prefetch_indices)
        self._ensure_warmup()

    def release_interest(self) -> None:
        self._pending_interest = ((), ())
        self._stream.release_interest()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._warmup_client_id)

    def refresh(self) -> None:
        self._stream.refresh()

    def load_page_source(
        self,
        page_index: int,
        preview_size: tuple[int, int],
        *,
        opening: bool,
    ) -> None:
        """Load and prepare one book page without exposing encoded bytes to the View."""

        book = self._book
        if book is None or page_index < 0:
            return
        request = (page_index, preview_size, opening)
        self._page_preview_request = request
        if self._source is None:
            self._pending_page_preview = request
            self._open_source()
            return
        self._start_page_preview(self._source, page_index, preview_size, opening=opening)

    def load_import_source(self, image_path: Path, preview_size: tuple[int, int]) -> None:
        """Read and prepare an imported image on the task executor."""

        self._pending_page_preview = None
        self._page_preview_request = None
        self._submit_preview(
            image_path.read_bytes,
            f"import:{image_path.name}",
            preview_size,
            opening=False,
        )

    def save_cover(self, crop_state: CoverCropState, output_size: tuple[int, int]) -> None:
        """Render from the retained original bytes and persist the edited cover."""

        book = self._book
        source_bytes = self._source_bytes
        source_token = self._source_token
        if book is None or source_bytes is None or source_token is None:
            self.save_failed.emit(RuntimeError("Cover source is unavailable"))
            return
        if crop_state.source_id != source_token:
            self.save_failed.emit(RuntimeError("Cover source changed before save"))
            return
        if self._save_task is not None:
            self._save_task.cancel()
        self._save_task = None
        generation = self._generation

        def success(path: Path) -> None:
            if generation != self._generation or self._book is None:
                return
            self._save_task = None
            self.cover_saved.emit(book.uuid, Path(path))

        def failure(error: Exception) -> None:
            if generation != self._generation:
                return
            self._save_task = None
            self.save_failed.emit(error)

        handle = self._submit(
            "save-edited-cover",
            lambda: self._thumbnail_service.save_edited_cover(
                book,
                source_bytes,
                crop_state,
                output_size,
            ),
            on_success=success,
            on_failure=failure,
            priority=TaskPriority.HIGH,
        )
        if handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self._save_task = handle

    def invalidate_source(self) -> None:
        """Discard an open picker source after the archive policy changes."""

        book = self._book
        if book is None:
            return
        self._generation += 1
        self._preview_generation += 1
        if self._source_task is not None:
            self._source_task.cancel()
        self._source_task = None
        if self._preview_task is not None:
            self._preview_task.cancel()
        self._preview_task = None
        self._source_bytes = None
        self._source_token = None
        self._pending_page_preview = self._page_preview_request
        if self._source is not None:
            self._source.close()
        self._source = None
        self._stream.cancel()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._warmup_client_id)
        self.source_ready.emit(book.uuid, 0)
        self._open_source()

    def cancel(self) -> None:
        self._generation += 1
        self._preview_generation += 1
        if self._source_task is not None:
            self._source_task.cancel()
        self._source_task = None
        if self._preview_task is not None:
            self._preview_task.cancel()
        self._preview_task = None
        if self._save_task is not None:
            self._save_task.cancel()
        self._save_task = None
        self._source_bytes = None
        self._source_token = None
        self._pending_page_preview = None
        self._page_preview_request = None
        if self._source is not None:
            self._source.close()
        self._source = None
        self._book = None
        self._pending_interest = ((), ())
        self._stream.cancel()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._warmup_client_id)

    def _start_page_preview(
        self,
        source: ThumbnailSourceHandle,
        page_index: int,
        preview_size: tuple[int, int],
        *,
        opening: bool,
    ) -> None:
        request = (page_index, preview_size, opening)
        self._pending_page_preview = None
        self._page_preview_request = request

        def load() -> bytes:
            page = source.read_page(page_index)
            image_bytes = getattr(page, "image_bytes", None) if page is not None else None
            if not isinstance(image_bytes, bytes):
                raise OSError("Cover source page is unavailable")
            return image_bytes

        self._submit_preview(
            load,
            f"page:{page_index + 1}",
            preview_size,
            opening=opening,
        )

    def _submit_preview(
        self,
        load_bytes,
        source_token: str,
        preview_size: tuple[int, int],
        *,
        opening: bool,
    ) -> None:  # noqa: ANN001 - worker callback has a simple bytes contract.
        book = self._book
        if book is None:
            return
        self._preview_generation += 1
        preview_generation = self._preview_generation
        source_generation = self._generation
        if self._preview_task is not None:
            self._preview_task.cancel()
        self._preview_task = None

        target = max(1, int(preview_size[0])), max(1, int(preview_size[1]))

        def prepare() -> _LoadedCoverSource:
            encoded = load_bytes()
            prepared = self._preview_renderer.prepare_preview(encoded, target, source_token)
            return _LoadedCoverSource(encoded, prepared, opening)

        def success(result: _LoadedCoverSource) -> None:
            if (
                preview_generation != self._preview_generation
                or source_generation != self._generation
                or self._book is None
                or self._book.uuid != book.uuid
            ):
                return
            self._preview_task = None
            self._source_bytes = result.encoded_bytes
            self._source_token = result.prepared.source_token
            self.preview_ready.emit(book.uuid, result.prepared, result.opening)

        def failure(error: Exception) -> None:
            if (
                preview_generation != self._preview_generation
                or source_generation != self._generation
            ):
                return
            self._preview_task = None
            self.preview_failed.emit(error, opening)

        handle = self._submit(
            "cover-editor-preview",
            prepare,
            on_success=success,
            on_failure=failure,
            priority=TaskPriority.HIGH,
        )
        if handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self._preview_task = handle

    def _submit(
        self,
        name: str,
        callback,
        *,
        on_success,
        on_failure,
        priority: TaskPriority,
        on_discard=None,
    ):  # noqa: ANN001, ANN202 - compatibility adapter for legacy test executors.
        try:
            return self._task_service.submit(
                name,
                callback,
                on_success=on_success,
                on_failure=on_failure,
                on_discard=on_discard,
                priority=priority,
            )
        except TypeError:
            return self._task_service.submit(
                name,
                callback,
                on_success=on_success,
                on_failure=on_failure,
            )

    def _open_source(self) -> None:
        book = self._book
        if book is None or self._source is not None or self._source_task is not None:
            return
        generation = self._generation

        def success(source: ThumbnailSourceHandle | None) -> None:
            if generation != self._generation or self._book is None:
                if source is not None:
                    source.close()
                return
            self._source_task = None
            self._source = source
            if source is None:
                self.source_ready.emit(self._book.uuid, 0)
                pending = self._pending_page_preview
                self._pending_page_preview = None
                if pending is not None:
                    self.preview_failed.emit(OSError("Cover source is unavailable"), pending[2])
                return
            thumbnail_service = self._thumbnail_service
            size = self._size

            def load(indices: tuple[int, ...], emit_item) -> None:  # noqa: ANN001
                thumbnail_service.stream_thumbnails(
                    source,
                    indices,
                    size,
                    lambda item: emit_item(ThumbnailStreamItem(item.page_index, item.image_bytes)),
                )

            self._stream.set_source(
                source.source_id,
                source.page_count,
                size,
                load,
                batch_size_for=source.preferred_batch_size,
            )
            self.source_ready.emit(self._book.uuid, source.page_count)
            visible, prefetch = self._pending_interest
            if visible or prefetch:
                self._stream.set_interest(visible, prefetch)
                self._ensure_warmup()
            pending = self._pending_page_preview
            if pending is not None:
                page_index, preview_size, opening = pending
                self._start_page_preview(
                    source,
                    page_index,
                    preview_size,
                    opening=opening,
                )

        def failure(error: Exception) -> None:
            if generation != self._generation:
                return
            self._source_task = None
            pending = self._pending_page_preview
            self._pending_page_preview = None
            if pending is not None:
                self.preview_failed.emit(error, pending[2])
            else:
                self.failed.emit(error)

        handle = self._submit(
            "cover-editor-thumbnail-source",
            lambda: self._thumbnail_service.open_thumbnail_source(book),
            on_success=success,
            on_failure=failure,
            on_discard=lambda source: source.close() if source is not None else None,
            priority=TaskPriority.HIGH,
        )
        if handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self._source_task = handle

    def _ensure_warmup(self) -> None:
        source = self._source
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
            self._warmup_client_id,
            limits=source.archive_limits,
            document_cache_key=source.persistent_cache_key,
            allow_persistent_cache=True,
            on_ready=self._stream.refresh,
        )
