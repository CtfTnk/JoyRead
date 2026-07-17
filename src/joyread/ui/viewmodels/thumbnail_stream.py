"""Viewport-driven thumbnail loading shared by image-oriented ViewModels."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from joyread.core.services.cache_service import ThumbnailCacheClient, ThumbnailCacheKey
from joyread.core.services.task_service import TaskHandle, TaskPriority, TaskService, TaskStatus
from joyread.ui.viewmodels.signals import Signal


logger = logging.getLogger(__name__)

ThumbnailEmitter = Callable[["ThumbnailStreamItem"], None]
ThumbnailLoader = Callable[[tuple[int, ...], ThumbnailEmitter], None]
BatchSizeProvider = Callable[[int], int]


@dataclass(frozen=True)
class ThumbnailStreamItem:
    page_index: int
    image_bytes: bytes


class ThumbnailStreamController:
    """Serial interest scheduler with shared-cache pinning.

    The controller never discovers viewport geometry itself. Views report the
    visible and one-row-prefetch indices; this class turns that small interest
    set into cache hits and at most one background task. Cold archive loaders
    may return a batch size greater than one, but each rendered item is emitted
    independently through ``thumbnail_ready``.
    """

    def __init__(
        self,
        task_service: TaskService,
        cache_client: ThumbnailCacheClient,
        *,
        task_name: str,
    ) -> None:
        self.thumbnail_ready: Signal[tuple[int, bytes]] = Signal(f"{task_name}.thumbnail_ready")
        self.thumbnail_failed: Signal[int] = Signal(f"{task_name}.thumbnail_failed")
        self._task_service = task_service
        self._cache = cache_client
        self._task_name = task_name
        self._source_id: str | None = None
        self._page_count = 0
        self._size = (1, 1)
        self._loader: ThumbnailLoader | None = None
        self._batch_size_for: BatchSizeProvider = lambda _index: 1
        self._visible: tuple[int, ...] = ()
        self._prefetch: tuple[int, ...] = ()
        self._interest: frozenset[int] = frozenset()
        self._queue: list[int] = []
        self._active_indices: tuple[int, ...] = ()
        self._handle: TaskHandle[object] | None = None
        self._generation = 0
        self._submitting = False
        self._pump_deferred = False

    @property
    def source_id(self) -> str | None:
        return self._source_id

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def active_indices(self) -> tuple[int, ...]:
        return self._active_indices

    def set_source(
        self,
        source_id: str,
        page_count: int,
        size: tuple[int, int],
        loader: ThumbnailLoader,
        *,
        batch_size_for: BatchSizeProvider | None = None,
    ) -> None:
        normalized_size = (max(1, int(size[0])), max(1, int(size[1])))
        if (
            source_id == self._source_id
            and page_count == self._page_count
            and normalized_size == self._size
            and loader is self._loader
        ):
            return
        self.cancel()
        self._source_id = source_id
        self._page_count = max(0, int(page_count))
        self._size = normalized_size
        self._loader = loader
        self._batch_size_for = batch_size_for or (lambda _index: 1)

    def set_interest(
        self,
        visible_indices: Iterable[int],
        prefetch_indices: Iterable[int] = (),
    ) -> None:
        if self._source_id is None or self._loader is None or self._page_count <= 0:
            self.release_interest()
            return
        visible = _unique_valid_indices(visible_indices, self._page_count)
        visible_set = set(visible)
        prefetch = tuple(
            index
            for index in _unique_valid_indices(prefetch_indices, self._page_count)
            if index not in visible_set
        )
        if visible == self._visible and prefetch == self._prefetch:
            return

        # A viewport change replaces the queue as one atomic generation. The
        # worker may already be inside archive I/O, so cancellation alone is
        # insufficient; the generation check also drops its late item signals.
        self._generation += 1
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._active_indices = ()
        self._visible = visible
        self._prefetch = prefetch
        self._interest = frozenset((*visible, *prefetch))
        self._cache.set_pins(frozenset(self._key(index) for index in self._interest))

        ordered = _center_out(visible)
        ordered.extend(prefetch)
        missing: list[int] = []
        for page_index in ordered:
            cached = self._cache.get(self._key(page_index))
            if cached is None:
                missing.append(page_index)
                continue
            self.thumbnail_ready.emit(page_index, cached)

        self._queue = missing
        self._pump()

    def release_interest(self) -> None:
        self._generation += 1
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._active_indices = ()
        self._queue = []
        self._visible = ()
        self._prefetch = ()
        self._interest = frozenset()
        self._cache.release()

    def cancel(self) -> None:
        self.release_interest()
        self._source_id = None
        self._page_count = 0
        self._loader = None
        self._batch_size_for = lambda _index: 1

    def refresh(self) -> None:
        visible = self._visible
        prefetch = self._prefetch
        self._visible = ()
        self._prefetch = ()
        self.set_interest(visible, prefetch)

    def _pump(self) -> None:
        if self._handle is not None and self._handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return
        self._handle = None
        self._active_indices = ()
        if self._loader is None or not self._queue:
            return

        first = self._queue[0]
        batch_size = max(1, min(8, int(self._batch_size_for(first))))
        selected = tuple(self._queue[:batch_size])
        self._queue = self._queue[len(selected) :]
        self._active_indices = selected
        generation = self._generation
        priority = TaskPriority.HIGH if any(index in self._visible for index in selected) else TaskPriority.NORMAL

        def work(emit_item: ThumbnailEmitter) -> None:
            assert self._loader is not None
            self._loader(selected, emit_item)

        submit_stream = getattr(self._task_service, "submit_stream", None)
        if callable(submit_stream):
            self._submitting = True
            try:
                handle = submit_stream(
                    f"{self._task_name}-{selected[0]}",
                    work,
                    on_item=lambda item, generation=generation: self._handle_item(generation, item),
                    on_success=lambda _result, generation=generation: self._handle_finished(generation),
                    on_failure=lambda _error, generation=generation: self._handle_failed(generation, selected),
                    priority=priority,
                )
                if not self._pump_deferred and handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                    self._handle = handle
            finally:
                self._submitting = False
                self._resume_deferred_pump()
            return

        # Compatibility for synchronous task fakes used by focused ViewModel
        # tests. Production always takes the streaming path above.
        items: list[ThumbnailStreamItem] = []

        def collect() -> None:
            work(items.append)

        submit = self._task_service.submit
        self._submitting = True
        try:
            try:
                handle = submit(
                    f"{self._task_name}-{selected[0]}",
                    collect,
                    on_success=lambda _result, generation=generation: self._handle_collected(generation, items),
                    on_failure=lambda _error, generation=generation: self._handle_failed(generation, selected),
                    priority=priority,
                )
            except TypeError:
                handle = submit(
                    f"{self._task_name}-{selected[0]}",
                    collect,
                    on_success=lambda _result, generation=generation: self._handle_collected(generation, items),
                    on_failure=lambda _error, generation=generation: self._handle_failed(generation, selected),
                )
            if not self._pump_deferred and handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                self._handle = handle
        finally:
            self._submitting = False
            self._resume_deferred_pump()

    def _handle_collected(self, generation: int, items: list[ThumbnailStreamItem]) -> None:
        for item in items:
            self._handle_item(generation, item)
        self._handle_finished(generation)

    def _handle_item(self, generation: int, item: ThumbnailStreamItem) -> None:
        if generation != self._generation or self._source_id is None:
            return
        page_index = int(item.page_index)
        if not 0 <= page_index < self._page_count:
            return
        self._cache.put(self._key(page_index), item.image_bytes)
        if page_index in self._interest:
            self.thumbnail_ready.emit(page_index, item.image_bytes)

    def _handle_finished(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._handle = None
        self._active_indices = ()
        if self._submitting:
            self._pump_deferred = True
        else:
            self._pump()

    def _handle_failed(self, generation: int, indices: tuple[int, ...]) -> None:
        if generation != self._generation:
            return
        for page_index in indices:
            if page_index in self._interest:
                self.thumbnail_failed.emit(page_index)
        self._handle = None
        self._active_indices = ()
        if self._submitting:
            self._pump_deferred = True
        else:
            self._pump()

    def _resume_deferred_pump(self) -> None:
        if not self._pump_deferred:
            return
        self._pump_deferred = False
        self._pump()

    def _key(self, page_index: int) -> ThumbnailCacheKey:
        assert self._source_id is not None
        return ThumbnailCacheKey(
            self._source_id,
            int(page_index),
            self._size[0],
            self._size[1],
        )


def _unique_valid_indices(indices: Iterable[int], page_count: int) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(index) for index in indices if 0 <= int(index) < page_count))


def _center_out(indices: tuple[int, ...]) -> list[int]:
    if len(indices) <= 1:
        return list(indices)
    ordered = sorted(indices)
    center = (ordered[0] + ordered[-1]) / 2.0
    return sorted(ordered, key=lambda index: (abs(index - center), index))
