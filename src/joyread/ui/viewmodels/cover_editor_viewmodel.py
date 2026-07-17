"""Viewport thumbnail state for the cover editor page picker."""

from __future__ import annotations

from joyread.core.models.book import Book
from joyread.core.services.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.core.services.task_service import TaskHandle, TaskPriority, TaskService, TaskStatus
from joyread.core.services.thumbnail_service import ThumbnailService, ThumbnailSourceHandle
from joyread.ui.viewmodels.signals import Signal
from joyread.ui.viewmodels.thumbnail_stream import ThumbnailStreamController, ThumbnailStreamItem


class CoverEditorThumbnailViewModel:
    """Keeps cover-page browsing work outside the MainWindow view."""

    def __init__(
        self,
        thumbnail_service: ThumbnailService,
        task_service: TaskService,
        archive_warmup_coordinator: ArchiveWarmupCoordinator | None = None,
    ) -> None:
        self.source_ready: Signal[tuple[str, int]] = Signal("cover-editor.source_ready")
        self.thumbnail_ready: Signal[tuple[int, bytes]] = Signal("cover-editor.thumbnail_ready")
        self.failed: Signal[Exception] = Signal("cover-editor.failed")
        self._thumbnail_service = thumbnail_service
        self._task_service = task_service
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
        self._size = (1, 1)
        self._pending_interest: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
        self._generation = 0

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

    def cancel(self) -> None:
        self._generation += 1
        if self._source_task is not None:
            self._source_task.cancel()
        self._source_task = None
        self._source = None
        self._book = None
        self._pending_interest = ((), ())
        self._stream.cancel()
        if self._archive_warmup_coordinator is not None:
            self._archive_warmup_coordinator.release(self._warmup_client_id)

    def _open_source(self) -> None:
        book = self._book
        if book is None or self._source is not None or self._source_task is not None:
            return
        generation = self._generation

        def success(source: ThumbnailSourceHandle | None) -> None:
            if generation != self._generation or self._book is None:
                return
            self._source_task = None
            self._source = source
            if source is None:
                self.source_ready.emit(self._book.uuid, 0)
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

        def failure(error: Exception) -> None:
            if generation != self._generation:
                return
            self._source_task = None
            self.failed.emit(error)

        try:
            handle = self._task_service.submit(
                "cover-editor-thumbnail-source",
                lambda: self._thumbnail_service.open_thumbnail_source(book),
                on_success=success,
                on_failure=failure,
                priority=TaskPriority.HIGH,
            )
        except TypeError:
            handle = self._task_service.submit(
                "cover-editor-thumbnail-source",
                lambda: self._thumbnail_service.open_thumbnail_source(book),
                on_success=success,
                on_failure=failure,
            )
        if handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self._source_task = handle

    def _ensure_warmup(self) -> None:
        source = self._source
        coordinator = self._archive_warmup_coordinator
        if source is None or coordinator is None:
            return
        access_mode = getattr(source.session, "access_mode", None)
        if getattr(access_mode, "value", access_mode) != "expensive_cold":
            return
        coordinator.acquire(
            source.source_path,
            self._warmup_client_id,
            nested_depth=source.nested_archive_max_depth,
            global_depth=source.archive_global_file_max_depth,
            on_ready=self._stream.refresh,
        )
